"""Frame-skip output (`Transport.show_frame` / `show_bottom_frame` / `show_title`): a
caller only records the newest content of a live message and returns at once; one frame
task pushes it when a slot is spare and no flood-wait runs, skipping content that was
replaced before it was sent. See AGENTS.md → Frame-skip output."""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter

from tgforge.base import kernel
from tgforge.base.kernel import Transport


class _Flood(TelegramRetryAfter):
    def __init__(self, retry_after=0):
        self.retry_after = retry_after


class _Rejected(TelegramBadRequest):
    def __init__(self, message="Bad Request: message to edit not found"):
        self.message = message

    def __str__(self):
        return self.message


class _Bot:
    """Records every call with its monotonic time; `flood` makes the next N calls of a
    method raise a flood-wait of `retry_after` seconds."""

    def __init__(self):
        self.calls: list[tuple[float, str, object]] = []
        self.flood: dict[str, list[float]] = {}
        self.reject: set[str] = set()
        self._next = 100

    def _hit(self, method, payload):
        self.calls.append((time.monotonic(), method, payload))
        pending = self.flood.get(method)
        if pending:
            raise _Flood(pending.pop(0))
        if method in self.reject:
            raise _Rejected()

    async def edit_message_text(self, text, **kw):
        self._hit("edit", text)
        return SimpleNamespace(message_id=kw["message_id"])

    async def send_message(self, chat_id, text, **kw):
        self._hit("send", text)
        self._next += 1
        return SimpleNamespace(message_id=self._next, message_thread_id=kw.get("message_thread_id"))

    async def delete_message(self, chat_id, message_id):
        self._hit("delete", message_id)
        return True

    async def edit_forum_topic(self, chat_id, message_thread_id, name):
        self._hit("title", name)
        return True

    def texts(self, method):
        return [payload for _t, m, payload in self.calls if m == method]


def test_flood_wait_holds_every_call_and_then_shows_only_the_newest_frame():
    async def scenario():
        bot = _Bot()
        t = Transport(bot=bot)
        bot.flood["edit"] = [0.3]
        t.show_frame(1, 50, "frame 1")
        await asyncio.sleep(0.05)  # frame 1 hits the flood-wait
        deadline = t.flood_until
        for n in range(2, 6):  # local work goes on; each call returns at once
            started = time.monotonic()
            t.show_frame(1, 50, f"frame {n}")
            assert time.monotonic() - started < 0.01
        await t.flush_frames()
        assert bot.texts("edit") == ["frame 1", "frame 5"]  # 2–4 skipped, never sent
        after = [at for at, method, _ in bot.calls][1:]
        assert all(at >= deadline - 0.01 for at in after)  # nothing before the deadline

    asyncio.run(scenario())


def test_an_essential_call_also_waits_out_a_flood_seen_by_a_frame():
    async def scenario():
        bot = _Bot()
        t = Transport(bot=bot)
        bot.flood["edit"] = [0.3]
        t.show_frame(1, 50, "frame")
        await asyncio.sleep(0.05)
        await t.send(1, "a reply")
        sent_at = next(at for at, method, _ in bot.calls if method == "send")
        assert sent_at >= t.flood_until - 0.01

    asyncio.run(scenario())


def test_an_unchanged_frame_makes_no_call():
    async def scenario():
        bot = _Bot()
        t = Transport(bot=bot)
        t.show_frame(1, 50, "same")
        await t.flush_frames()
        t.show_frame(1, 50, "same")
        await t.flush_frames()
        assert bot.texts("edit") == ["same"]

    asyncio.run(scenario())


def test_a_frame_future_reports_landed_and_rejected():
    async def scenario():
        bot = _Bot()
        t = Transport(bot=bot)
        assert await t.show_frame(1, 50, "ok") is True
        bot.reject.add("edit")
        assert await t.show_frame(1, 51, "gone") is False  # Telegram rejected the edit

    asyncio.run(scenario())


def test_a_must_land_frame_survives_a_flood_and_is_pushed_first():
    async def scenario():
        bot = _Bot()
        t = Transport(bot=bot)
        bot.flood["edit"] = [0.05]
        t.show_frame(1, 50, "spinner")
        final = t.show_frame(1, 60, "final", must_land=True)
        assert await final is True  # pending through the flood-wait until it landed
        assert bot.texts("edit")[0] == "final"  # ahead of the older live frame

    asyncio.run(scenario())


def test_deleting_a_message_drops_its_pending_frame():
    async def scenario():
        bot = _Bot()
        t = Transport(bot=bot)
        waiter = t.show_frame(1, 50, "never shown")
        await t.delete_msg(1, 50)
        await t.flush_frames()
        assert bot.texts("edit") == []
        assert await waiter is False

    asyncio.run(scenario())


def test_frames_yield_to_a_pending_essential_call(monkeypatch):
    async def scenario():
        bot = _Bot()
        t = Transport(bot=bot, pace_per_min=6000, pace_burst=1)
        t._pacer.waiting = 1  # an essential reply is pending
        t.show_frame(1, 50, "spinner")
        await asyncio.sleep(0.3)
        assert bot.texts("edit") == []  # the frame did not take the reply's slot
        t._pacer.waiting = 0
        await t.flush_frames()
        assert bot.texts("edit") == ["spinner"]

    asyncio.run(scenario())


def test_a_button_tap_answer_uses_no_pacer_slot():
    async def scenario():
        answered = []
        t = Transport(bot=_Bot(), pace_per_min=1, pace_burst=1)
        t._pacer.tokens = 0  # budget spent: a paced call would wait a minute
        started = time.monotonic()
        await t._call(lambda: answered.append(1) or asyncio.sleep(0), paced=False)
        assert answered and time.monotonic() - started < 0.5
        assert t._pacer.tokens < 1  # no slot taken or refunded

    asyncio.run(scenario())


def test_a_button_tap_answer_still_waits_out_a_flood():
    async def scenario():
        t = Transport(bot=_Bot())
        t.flood_until = time.monotonic() + 0.2
        answered = []
        await t._call(lambda: answered.append(time.monotonic()) or asyncio.sleep(0), paced=False)
        assert answered[0] >= t.flood_until - 0.01

    asyncio.run(scenario())


# ── Bottom frames ──────────────────────────────────────────────────


def test_a_bottom_frame_is_re_sent_below_a_newer_message_and_the_old_one_deleted():
    async def scenario():
        bot = _Bot()
        t = Transport(bot=bot)
        t.show_bottom_frame(1, 7, "panel", "rows v1")
        await t.flush_frames()
        first = t.bottom_frame_id("panel")
        await t.send(1, "a later message", thread_id=7)
        await t.flush_frames()
        second = t.bottom_frame_id("panel")
        assert second > first  # below the later message
        assert first in bot.texts("delete")  # the buried copy went
        t.show_bottom_frame(1, 7, "panel", "rows v2")
        await t.flush_frames()
        assert bot.texts("edit") == ["rows v2"]  # still the bottom: edited in place

    asyncio.run(scenario())


def test_a_dropped_bottom_re_send_is_retried():
    async def scenario():
        bot = _Bot()
        t = Transport(bot=bot)
        t.show_bottom_frame(1, 7, "panel", "rows")
        await t.flush_frames()
        bot.flood["send"] = [0.05]
        bot._next = 10_000
        t.note_message(7, 10_000)  # a newer message in the thread
        await t.flush_frames()
        assert t.bottom_frame_id("panel") > 10_000
        assert bot.texts("send").count("rows") == 3  # first send, the flooded try, the retry

    asyncio.run(scenario())


def test_a_retired_bottom_frame_is_deleted():
    async def scenario():
        bot = _Bot()
        t = Transport(bot=bot)
        t.show_bottom_frame(1, 7, "panel", "rows")
        await t.flush_frames()
        sent = t.bottom_frame_id("panel")
        t.show_bottom_frame(1, 7, "panel", None)
        await t.flush_frames()
        assert bot.texts("delete") == [sent]
        assert t.bottom_frame_id("panel") is None

    asyncio.run(scenario())


# ── Titles ─────────────────────────────────────────────────────────


def test_a_burst_of_titles_gives_one_rename_to_the_last(monkeypatch):
    monkeypatch.setattr(kernel, "TITLE_MIN_INTERVAL", 0.3)

    async def scenario():
        bot = _Bot()
        t = Transport(bot=bot)
        t.show_title(1, 7, "start")
        await t.flush_frames()
        for name in ("a", "b", "c", "d", "e"):
            t.show_title(1, 7, name)
            await asyncio.sleep(0.01)
        await t.flush_frames()
        assert bot.texts("title") == ["start", "e"]
        landed = [at for at, method, _ in bot.calls if method == "title"]
        assert landed[1] - landed[0] >= 0.29  # renames of one topic stay spaced

    asyncio.run(scenario())


def test_an_immediate_title_skips_the_spacing(monkeypatch):
    monkeypatch.setattr(kernel, "TITLE_MIN_INTERVAL", 30)

    async def scenario():
        bot = _Bot()
        t = Transport(bot=bot)
        t.show_title(1, 7, "start")
        await t.flush_frames()
        t.show_title(1, 7, "explicit", immediate=True)
        await asyncio.wait_for(t.flush_frames(), 1)
        assert bot.texts("title") == ["start", "explicit"]

    asyncio.run(scenario())


def test_closing_a_window_forgets_its_frames_and_title(tmp_path):
    from tgforge.base.kernel import _CoreTopic
    from tgforge.testing import TestClient

    async def scenario():
        c = TestClient(home=str(tmp_path))
        inst = await c.core.open_window(c.core.chat_id, _CoreTopic, "w")
        inst.show_bottom_frame("panel", "rows")
        c.core.show_title(c.core.chat_id, inst.thread_id, "pending name")
        await c.core.close_window(inst.thread_id)
        assert inst.bottom_frame_id("panel") is None
        assert inst.thread_id not in c.core._titles

    asyncio.run(scenario())
