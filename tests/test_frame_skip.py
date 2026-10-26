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
from tgforge.testing import MockBot


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
        assert await t.land_frame(1, 50, "ok") is True
        bot.reject.add("edit")
        assert await t.land_frame(1, 51, "gone") is False  # Telegram rejected the edit

    asyncio.run(scenario())


def test_a_must_land_frame_survives_a_flood_and_is_pushed_first():
    async def scenario():
        bot = _Bot()
        t = Transport(bot=bot)
        bot.flood["edit"] = [0.05]
        t.show_frame(1, 50, "spinner")
        final = t.land_frame(1, 60, "final")
        assert await final is True  # pending through the flood-wait until it landed
        assert bot.texts("edit")[0] == "final"  # ahead of the older live frame

    asyncio.run(scenario())


def test_deleting_a_message_drops_its_pending_frame():
    async def scenario():
        bot = _Bot()
        t = Transport(bot=bot)
        waiter = t.land_frame(1, 50, "never shown")
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


# ── Review round 2 ─────────────────────────────────────────────────


class _SlowEditBot(_Bot):
    async def edit_message_text(self, text, **kw):
        await asyncio.sleep(0.05)  # the HTTP round trip
        return await super().edit_message_text(text, **kw)


def test_a_flood_recorded_during_the_slot_wait_holds_the_essential_call():
    """A frame push takes the only slot and gets a flood-wait while an essential send
    sleeps in the pacer: the send re-checks the deadline and waits it out."""

    async def scenario():
        bot = _SlowEditBot()
        t = Transport(bot=bot, pace_per_min=600, pace_burst=1)  # refill every 0.1 s
        bot.flood["edit"] = [0.5]
        t.show_frame(1, 50, "frame")  # the frame push takes the only token
        await asyncio.sleep(0.01)
        await asyncio.wait_for(t.send(1, "reply", thread_id=7), 5)
        sent_at = next(at for at, method, _ in bot.calls if method == "send")
        assert sent_at >= t.flood_until - 0.01  # not inside the flood window

    asyncio.run(scenario())


def test_a_flood_recorded_during_a_frames_slot_wait_holds_the_frame():
    """The frame task sleeps for a spare slot; a flood recorded meanwhile (set here
    directly: the deadline is all the check reads) holds the push until it ends."""

    async def scenario():
        bot = _Bot()
        t = Transport(bot=bot, pace_per_min=600, pace_burst=1)  # refill every 0.1 s
        t._pacer.tokens = 0  # the frame must wait for a slot
        t.show_frame(1, 50, "frame")
        await asyncio.sleep(0.02)  # the frame task is in its slot wait
        t.flood_until = time.monotonic() + 0.4
        await asyncio.wait_for(t.flush_frames(), 5)
        pushed_at = next(at for at, method, _ in bot.calls if method == "edit")
        assert pushed_at >= t.flood_until - 0.01

    asyncio.run(scenario())


def test_a_must_land_frame_keeps_its_priority_after_a_flood():
    async def scenario():
        bot = _Bot()
        t = Transport(bot=bot)
        bot.flood["edit"] = [0.2]
        t.show_frame(1, 60, "live view A")
        t.show_frame(1, 70, "live view B")
        final = t.land_frame(1, 50, "final")  # pushed first, flood-waited
        assert await asyncio.wait_for(final, 5) is True
        assert bot.texts("edit")[:2] == ["final", "final"]  # retried before A and B

    asyncio.run(scenario())


def test_a_live_frame_creates_no_future():
    async def scenario():
        t = Transport(bot=_Bot())
        assert t.show_frame(1, 50, "live") is None
        assert t._frames[("msg", 1, 50)].waiters == []  # nothing accumulates per frame
        await t.flush_frames()

    asyncio.run(scenario())


def test_a_window_closed_with_a_pending_message_frame_pushes_nothing(tmp_path):
    from tgforge.base.kernel import _CoreTopic
    from tgforge.testing import TestClient

    async def scenario():
        c = TestClient(home=str(tmp_path))
        inst = await c.core.open_window(c.core.chat_id, _CoreTopic, "w")
        c.core.flood_until = time.monotonic() + 0.2  # hold the frame task
        inst.show_frame(4242, "a view never shown")
        await c.core.close_window(inst.thread_id)
        await asyncio.wait_for(c.core.flush_frames(), 2)
        assert all(mid != 4242 for mid, _ in c.bot.edits)

    asyncio.run(scenario())


def test_a_failed_bottom_re_send_is_retried_without_a_later_update():
    async def scenario():
        bot = _Bot()
        t = Transport(bot=bot)
        failures = {"left": 1}
        real_send = bot.send_message

        async def send(chat_id, text, **kw):
            if failures["left"]:
                failures["left"] -= 1
                raise _Rejected("Bad Request: something transient")
            return await real_send(chat_id, text, **kw)

        bot.send_message = send
        t.show_bottom_frame(1, 7, "panel", "rows")  # one record, no later tick
        await asyncio.wait_for(t.flush_frames(), 2)
        assert t.bottom_frame_id("panel") is not None

    asyncio.run(scenario())


def test_a_lasting_bottom_re_send_failure_stops_retrying():
    from tgforge.base.kernel import BOTTOM_RESEND_ATTEMPTS

    async def scenario():
        bot = _Bot()
        t = Transport(bot=bot)
        bot.reject.add("send")
        t.show_bottom_frame(1, 7, "panel", "rows")
        await asyncio.wait_for(t.flush_frames(), 2)  # returns: no endless retry
        assert len(bot.texts("send")) == BOTTOM_RESEND_ATTEMPTS
        assert t.bottom_frame_id("panel") is None

    asyncio.run(scenario())


def test_a_flood_waited_delete_of_the_buried_panel_copy_is_retried():
    async def scenario():
        bot = _Bot()
        t = Transport(bot=bot)
        t.show_bottom_frame(1, 7, "panel", "rows")
        await t.flush_frames()
        first = t.bottom_frame_id("panel")
        bot.flood["delete"] = [0.05]
        await t.send(1, "a later message", thread_id=7)
        await asyncio.wait_for(t.flush_frames(), 2)
        assert bot.texts("delete").count(first) == 2  # flooded, then deleted

    asyncio.run(scenario())


def test_a_flood_waited_retire_delete_is_retried():
    async def scenario():
        bot = _Bot()
        t = Transport(bot=bot)
        t.show_bottom_frame(1, 7, "panel", "rows")
        await t.flush_frames()
        sent = t.bottom_frame_id("panel")
        bot.flood["delete"] = [0.05]
        t.show_bottom_frame(1, 7, "panel", None)
        await asyncio.wait_for(t.flush_frames(), 2)
        assert bot.texts("delete") == [sent, sent]
        assert t.bottom_frame_id("panel") is None

    asyncio.run(scenario())


def test_a_markdown_rejected_panel_re_sends_plain_through_a_flood():
    async def scenario():
        bot = _Bot()
        t = Transport(bot=bot)
        calls = []
        real_send = bot.send_message
        floods = {"plain": 1}

        async def send(chat_id, text, **kw):
            calls.append(kw.get("parse_mode"))
            if kw.get("parse_mode") == "MarkdownV2":
                raise _Rejected("Bad Request: can't parse entities")
            if floods["plain"]:
                floods["plain"] -= 1
                raise _Flood(0.05)
            return await real_send(chat_id, text, **kw)

        bot.send_message = send
        t.show_bottom_frame(1, 7, "panel", "rows", "*rows*")
        await asyncio.wait_for(t.flush_frames(), 2)
        assert t.bottom_frame_id("panel") is not None  # the plain copy landed
        assert calls[:2] == ["MarkdownV2", None]  # md refused, plain flooded, then retried

    asyncio.run(scenario())


def test_a_lasting_failure_retries_again_after_new_content():
    from tgforge.base.kernel import BOTTOM_RESEND_ATTEMPTS

    async def scenario():
        bot = _Bot()
        t = Transport(bot=bot)
        bot.reject.add("send")
        t.show_bottom_frame(1, 7, "panel", "rows")
        await asyncio.wait_for(t.flush_frames(), 2)
        t.show_bottom_frame(1, 7, "panel", "rows, later")  # new content: a fresh burst
        await asyncio.wait_for(t.flush_frames(), 2)
        assert len(bot.texts("send")) == 2 * BOTTOM_RESEND_ATTEMPTS

    asyncio.run(scenario())


def test_each_frame_push_logs_its_delay_and_replaced_count(caplog):
    async def scenario():
        bot = _Bot()
        t = Transport(bot=bot)
        for n in range(3):  # recorded before the frame task runs: one push, 2 replaced
            t.show_frame(1, 50, f"frame {n}", thread_id=9)
        t.show_title(1, 7, "a name")
        await t.flush_frames()

    with caplog.at_level("INFO", logger="tgforge"):
        asyncio.run(scenario())
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("frame push")]
    assert len(lines) == 2
    assert lines[0].startswith("frame push msg 50 thread 9: ")
    assert "s after first recorded (slot wait " in lines[0]
    assert "2 replaced, 1 still pending" in lines[0]
    assert lines[1].startswith("frame push title thread 7: ")
    assert "0 replaced, 0 still pending" in lines[1]


def test_a_bottom_frame_keeps_its_keyboard_when_re_sent():
    async def scenario():
        bot = _Bot()
        sent_markup = []
        real_send = bot.send_message

        async def send_message(chat_id, text, **kw):
            sent_markup.append(kw.get("reply_markup"))
            return await real_send(chat_id, text, **kw)

        bot.send_message = send_message
        t = Transport(bot=bot)
        t.show_bottom_frame(1, 7, "view", "the view", reply_markup="KEYS")
        await t.flush_frames()
        t.note_message(7, bot._next + 1)  # a newer message below it
        await t.flush_frames()
        assert bot.texts("send") == ["the view", "the view"]  # re-sent below
        assert sent_markup == ["KEYS", "KEYS"]  # the buttons came along

    asyncio.run(scenario())


def test_a_message_that_does_not_bury_leaves_the_bottom_frame_in_place():
    async def scenario():
        bot = _Bot()
        t = Transport(bot=bot)
        t.show_bottom_frame(1, 7, "view", "the view")
        await t.flush_frames()
        await t.send(1, "a notice", thread_id=7, buries_bottom=False)
        await t.flush_frames()
        assert bot.texts("send") == ["the view", "a notice"]  # no re-send, no delete
        assert bot.texts("delete") == []
        await t.send(1, "a reply", thread_id=7)
        await t.flush_frames()
        assert bot.texts("send")[-1] == "the view"  # an ordinary message does re-send it

    asyncio.run(scenario())


# ── Moving a buried bottom frame: one paired step ──────────────────


def _record_pacer(t: Transport, events: list) -> None:
    """Log every pacer slot taken into `events`, next to the bot's own calls."""
    pacer = t._pacer
    for name in ("take", "take_spare"):
        real = getattr(pacer, name)

        async def wait(real=real, name=name):
            await real()
            events.append((name, None))

        setattr(pacer, name, wait)
    real_now = pacer.take_spare_now

    def take_now():
        taken = real_now()
        if taken:
            events.append(("take_spare_now", None))
        return taken

    pacer.take_spare_now = take_now


def _logging_mock_bot(events: list) -> MockBot:
    bot = MockBot()
    real_send, real_delete = bot.send_message, bot.delete_message

    async def send_message(chat_id, text, **kw):
        result = await real_send(chat_id, text, **kw)
        events.append(("send", result.message_id))
        return result

    async def delete_message(chat_id, message_id):
        events.append(("delete", message_id))
        return await real_delete(chat_id, message_id)

    bot.send_message, bot.delete_message = send_message, delete_message
    return bot


def _is_panel(text: str) -> bool:
    return text.startswith("rows")


def test_a_buried_panel_is_deleted_then_re_sent_with_no_slot_wait_between():
    async def scenario():
        events = []
        bot = _logging_mock_bot(events)
        t = Transport(bot=bot)
        _record_pacer(t, events)
        t.show_bottom_frame(1, 7, "panel", "rows")
        await t.flush_frames()
        first = t.bottom_frame_id("panel")
        events.clear()
        t.note_message(7, first + 1)  # a newer message buries it
        await t.flush_frames()
        second = t.bottom_frame_id("panel")
        assert events[-4:] == [
            ("take_spare", None),
            ("take_spare_now", None),  # the second slot, free at the same moment
            ("delete", first),
            ("send", second),
        ]

    asyncio.run(scenario())


def test_the_paired_move_lands_both_calls_inside_one_slot_wait():
    async def scenario():
        bot = _Bot()
        t = Transport(bot=bot, pace_per_min=600, pace_burst=3)  # one slot per 0.1 s
        t.show_bottom_frame(1, 7, "panel", "rows")
        await t.flush_frames()
        first = t.bottom_frame_id("panel")
        t._pacer.tokens = 0.0  # the bucket is empty when the panel is buried
        t.note_message(7, first + 1)
        started = time.monotonic()
        await t.flush_frames()
        (deleted_at,) = [at for at, m, p in bot.calls if m == "delete"]
        sent_at = [at for at, m, _p in bot.calls if m == "send"][-1]
        assert deleted_at - started >= 0.18  # waited for two slots, not one
        assert 0 <= sent_at - deleted_at < 0.05  # no slot wait between the two calls

    asyncio.run(scenario())


def test_take_spare_now_takes_only_a_free_spare_slot_and_never_waits():
    pacer = kernel._Pacer(600, 3)  # one slot per 0.1 s
    pacer.tokens = 1.0
    assert pacer.take_spare_now() and pacer.tokens < 0.1
    assert not pacer.take_spare_now()  # none free: no wait, no take
    pacer.tokens, pacer.waiting = 3.0, 1  # an essential call is pending
    assert not pacer.take_spare_now() and pacer.tokens > 2.9


def test_spare_delay_is_the_refill_time_to_the_wanted_slots():
    pacer = kernel._Pacer(600, 3)  # one slot per 0.1 s
    pacer.tokens = 1.0
    assert 0.09 <= pacer.spare_delay(2) <= 0.1
    pacer.tokens = 3.0
    assert pacer.spare_delay(2) == kernel.SPARE_SLOT_POLL  # free now: one poll, no spin
    assert kernel._Pacer(0, 3).spare_delay(2) == 0.0  # pacing off


def test_a_burst_of_three_messages_never_shows_two_panels():
    async def scenario():
        bot = MockBot()
        t = Transport(bot=bot, pace_per_min=1200, pace_burst=3)
        t.show_bottom_frame(1, 7, "panel", "rows")
        await t.flush_frames()
        for n in range(3):
            await t.send(1, f"message {n}", thread_id=7)
            await asyncio.sleep(0.05)  # the frame task moves the panel between them
        await t.flush_frames()
        assert bot.peak_on_screen(_is_panel) == 1
        assert bot.created[-1][2] == "rows"  # one panel, at the bottom
        assert len(bot.created) - len(bot.deleted) == 4  # three messages + one panel

    asyncio.run(scenario())


def test_a_flood_between_the_delete_and_the_send_still_ends_with_one_panel():
    async def scenario():
        bot = MockBot()
        real_send = bot.send_message
        floods = {"left": 0}

        async def send_message(chat_id, text, **kw):
            if text == "rows" and floods["left"]:
                floods["left"] -= 1
                raise _Flood(0.05)
            return await real_send(chat_id, text, **kw)

        bot.send_message = send_message
        t = Transport(bot=bot)
        t.show_bottom_frame(1, 7, "panel", "rows")
        await t.flush_frames()
        first = t.bottom_frame_id("panel")
        floods["left"] = 1  # the move's send hits a flood-wait after its delete landed
        await t.send(1, "later", thread_id=7)
        await asyncio.wait_for(t.flush_frames(), 2)
        assert bot.deleted == [first]
        assert bot.peak_on_screen(_is_panel) == 1
        assert bot.created[-1] == (t.bottom_frame_id("panel"), 7, "rows")  # back at the bottom

    asyncio.run(scenario())


def test_a_flood_waited_delete_keeps_the_old_panel_until_the_move_retries():
    async def scenario():
        events = []
        bot = _Bot()
        t = Transport(bot=bot)
        _record_pacer(t, events)
        t.show_bottom_frame(1, 7, "panel", "rows")
        await t.flush_frames()
        first = t.bottom_frame_id("panel")
        bot.flood["delete"] = [0.05]
        await t.send(1, "later", thread_id=7)
        await asyncio.wait_for(t.flush_frames(), 2)
        methods = [m for _at, m, _p in bot.calls]
        assert methods == ["send", "send", "delete", "delete", "send"]  # no send after the flood
        assert t.bottom_frame_id("panel") > first
        assert [e for e in events if e[0] == "take_spare_now"] == [("take_spare_now", None)] * 2

    asyncio.run(scenario())


def test_a_failed_re_send_after_the_delete_retries_up_to_the_attempt_limit():
    from tgforge.base.kernel import BOTTOM_RESEND_ATTEMPTS

    async def scenario():
        bot = MockBot()
        real_send = bot.send_message
        failures = {"left": 0}
        tried = []

        async def send_message(chat_id, text, **kw):
            if text == "rows":
                tried.append(text)
            if text == "rows" and failures["left"]:
                failures["left"] -= 1
                raise _Rejected("Bad Request: something transient")
            return await real_send(chat_id, text, **kw)

        bot.send_message = send_message
        t = Transport(bot=bot)
        t.show_bottom_frame(1, 7, "panel", "rows")
        await t.flush_frames()
        first = t.bottom_frame_id("panel")
        failures["left"] = BOTTOM_RESEND_ATTEMPTS - 1  # the last allowed attempt lands
        await t.send(1, "later", thread_id=7)
        await asyncio.wait_for(t.flush_frames(), 2)
        assert len(tried) == 1 + BOTTOM_RESEND_ATTEMPTS
        assert bot.deleted == [first]
        assert bot.peak_on_screen(_is_panel) == 1
        assert bot.created[-1] == (t.bottom_frame_id("panel"), 7, "rows")

        tried.clear()
        failures["left"] = 10**6  # a lasting rejection stops at the limit
        await t.send(1, "later again", thread_id=7)
        await asyncio.wait_for(t.flush_frames(), 2)
        assert len(tried) == BOTTOM_RESEND_ATTEMPTS

    asyncio.run(scenario())


def test_a_panel_left_with_no_copy_is_re_sent_by_the_next_newer_message():
    from tgforge.base.kernel import BOTTOM_RESEND_ATTEMPTS

    async def scenario():
        bot = MockBot()
        real_send = bot.send_message
        failures = {"left": 0}

        async def send_message(chat_id, text, **kw):
            if text == "rows" and failures["left"]:
                failures["left"] -= 1
                raise _Rejected("Bad Request: something transient")
            return await real_send(chat_id, text, **kw)

        bot.send_message = send_message
        t = Transport(bot=bot)
        t.show_bottom_frame(1, 7, "panel", "rows")
        await t.flush_frames()
        failures["left"] = BOTTOM_RESEND_ATTEMPTS  # the move's send gives up
        await t.send(1, "later", thread_id=7)
        await asyncio.wait_for(t.flush_frames(), 2)
        assert t.bottom_frame_id("panel") is None  # old copy deleted, no new one
        await t.send(1, "even later", thread_id=7)  # same content, a newer message
        await asyncio.wait_for(t.flush_frames(), 2)
        assert bot.created[-1] == (t.bottom_frame_id("panel"), 7, "rows")
        assert bot.peak_on_screen(_is_panel) == 1

    asyncio.run(scenario())


def test_a_move_logs_its_gap_from_the_delete_to_the_landed_send(caplog):
    async def scenario():
        bot = MockBot()
        t = Transport(bot=bot)
        t.show_bottom_frame(1, 7, "panel", "rows")
        await t.flush_frames()
        await t.send(1, "later", thread_id=7)
        await t.flush_frames()

    with caplog.at_level("INFO", logger="tgforge"):
        asyncio.run(scenario())
    lines = [r.getMessage() for r in caplog.records if "re-anchored" in r.getMessage()]
    assert lines[0] == "bottom frame panel: re-anchored None → 1001"
    assert lines[1].startswith("bottom frame panel: re-anchored 1001 → 1003 (gap ")


# ── A delete that fails for a reason other than a flood-wait ──────


class _BadRequest(TelegramBadRequest):
    def __init__(self, message):
        self.message = message

    def __str__(self):
        return self.message


def _bot_with_failing_delete(make_error):
    """A MockBot whose delete raises `make_error()` while `state["fail"]`, leaving the
    message on screen."""
    bot = MockBot()
    real_delete = bot.delete_message
    state = {"fail": False}

    async def delete_message(chat_id, message_id):
        if state["fail"]:
            raise make_error()
        return await real_delete(chat_id, message_id)

    bot.delete_message = delete_message
    return bot, state


def test_delete_of_an_already_gone_copy_still_ends_with_one_panel():
    async def scenario():
        bot, state = _bot_with_failing_delete(
            lambda: _BadRequest("Bad Request: message to delete not found")
        )
        t = Transport(bot=bot)
        t.show_bottom_frame(1, 7, "panel", "rows")
        await t.flush_frames()
        first = t.bottom_frame_id("panel")
        bot.screen_log.append(("delete", first, None))  # the user deleted it by hand
        state["fail"] = True
        await t.send(1, "later", thread_id=7)
        await asyncio.wait_for(t.flush_frames(), 2)
        assert bot.on_screen(_is_panel) == [t.bottom_frame_id("panel")]
        assert t.bottom_frame_id("panel") > first

    asyncio.run(scenario())


def test_a_lasting_4xx_on_the_delete_never_leaves_two_panels():
    """E.g. "message can't be deleted" (a copy older than 48 h without the delete
    right): the old copy is edited to a stub, so the thread keeps one panel."""

    async def scenario():
        bot, state = _bot_with_failing_delete(
            lambda: _BadRequest("Bad Request: message can't be deleted")
        )
        t = Transport(bot=bot)
        t.show_bottom_frame(1, 7, "panel", "rows", reply_markup="KEYS")
        await t.flush_frames()
        state["fail"] = True
        for n in range(3):
            await t.send(1, f"later {n}", thread_id=7)
            await asyncio.wait_for(t.flush_frames(), 2)
        assert len(bot.on_screen(_is_panel)) == 1, f"panels on screen: {bot.on_screen(_is_panel)}"
        assert bot.peak_on_screen(_is_panel) == 1
        assert [text for _m, text in bot.edits] == [kernel.STUB_TEXT] * 3
        assert bot.created[-1] == (t.bottom_frame_id("panel"), 7, "rows")

    asyncio.run(scenario())


def test_the_stub_edit_drops_the_old_copys_keyboard():
    async def scenario():
        bot, state = _bot_with_failing_delete(
            lambda: _BadRequest("Bad Request: message can't be deleted")
        )
        stub_kw = []
        real_edit = bot.edit_message_text

        async def edit_message_text(text, **kw):
            stub_kw.append(kw)
            return await real_edit(text, **kw)

        bot.edit_message_text = edit_message_text
        t = Transport(bot=bot)
        t.show_bottom_frame(1, 7, "view", "rows", reply_markup="KEYS")
        await t.flush_frames()
        first = t.bottom_frame_id("view")
        state["fail"] = True
        await t.send(1, "later", thread_id=7)
        await asyncio.wait_for(t.flush_frames(), 2)
        assert stub_kw == [{"chat_id": 1, "message_id": first}]  # no reply_markup: no buttons
        assert bot.last_markup == "KEYS"  # the new copy carries them

    asyncio.run(scenario())


def test_an_undeletable_copy_that_cannot_be_stubbed_stays_the_one_copy():
    async def scenario():
        bot, state = _bot_with_failing_delete(
            lambda: _BadRequest("Bad Request: message can't be deleted")
        )

        async def edit_message_text(text, **kw):
            raise _BadRequest("Bad Request: message can't be edited")

        bot.edit_message_text = edit_message_text
        t = Transport(bot=bot)
        t.show_bottom_frame(1, 7, "panel", "rows")
        await t.flush_frames()
        first = t.bottom_frame_id("panel")
        state["fail"] = True
        await t.send(1, "later", thread_id=7)
        await asyncio.wait_for(t.flush_frames(), 2)
        assert bot.on_screen(_is_panel) == [first]  # buried, but never a second copy
        assert t.bottom_frame_id("panel") == first

    asyncio.run(scenario())


def test_a_delete_that_exhausts_its_network_retries_never_leaves_two_panels(monkeypatch):
    from aiogram.exceptions import TelegramNetworkError
    from aiogram.methods import DeleteMessage

    monkeypatch.setattr(kernel, "TRANSIENT_BACKOFF", 0.001)

    async def scenario():
        method = DeleteMessage(chat_id=1, message_id=1)
        bot, state = _bot_with_failing_delete(lambda: TelegramNetworkError(method, "timeout"))
        t = Transport(bot=bot)
        t.show_bottom_frame(1, 7, "panel", "rows")
        await t.flush_frames()
        first = t.bottom_frame_id("panel")
        state["fail"] = True
        await t.send(1, "later", thread_id=7)
        await asyncio.wait_for(t.flush_frames(), 2)  # bounded: the move gives up
        assert bot.on_screen(_is_panel) == [first], f"panels on screen: {bot.on_screen(_is_panel)}"
        state["fail"] = False  # the network is back: the next newer message moves it
        await t.send(1, "even later", thread_id=7)
        await asyncio.wait_for(t.flush_frames(), 2)
        assert bot.on_screen(_is_panel) == [t.bottom_frame_id("panel")]
        assert bot.created[-1][2] == "rows" and bot.peak_on_screen(_is_panel) == 1

    asyncio.run(scenario())


# ── The move never holds back a must-land frame or stale content ───


def test_a_finalizing_edit_recorded_during_the_pair_wait_is_not_held_behind_it():
    """A land_frame recorded while the frame task waits for the move's two slots
    lands after one slot, not after the pair plus the move."""

    async def scenario():
        bot = MockBot()
        t = Transport(bot=bot, pace_per_min=600, pace_burst=3)  # one slot per 0.1 s
        card = await t.send(1, "card", thread_id=7)
        t.show_bottom_frame(1, 7, "panel", "rows")
        await t.flush_frames()
        t._pacer.tokens = 0.0
        t.note_message(7, t.bottom_frame_id("panel") + 1)
        await asyncio.sleep(0.12)  # the frame task picked the move; it waits for a pair
        started = time.monotonic()
        landed = t.land_frame(1, card, "final", thread_id=7)
        assert await asyncio.wait_for(landed, 2)
        assert time.monotonic() - started < 0.15, time.monotonic() - started
        await asyncio.wait_for(t.flush_frames(), 2)
        assert bot.on_screen(_is_panel) == [t.bottom_frame_id("panel")]

    asyncio.run(scenario())


def test_content_recorded_during_the_pair_wait_goes_out_in_the_move_itself():
    """The panel ticks its clock; a new content recorded while the move waits for its
    pair rides the move's send, not a follow-up edit."""

    async def scenario():
        bot = MockBot()
        t = Transport(bot=bot, pace_per_min=600, pace_burst=3)
        t.show_bottom_frame(1, 7, "panel", "rows 0s")
        await t.flush_frames()
        t._pacer.tokens = 0.0
        t.note_message(7, t.bottom_frame_id("panel") + 1)
        await asyncio.sleep(0.12)  # inside the pair wait
        t.show_bottom_frame(1, 7, "panel", "rows 1s")
        await asyncio.wait_for(t.flush_frames(), 2)
        assert bot.created[-1][2] == "rows 1s"
        assert bot.edits == []

    asyncio.run(scenario())


def test_a_move_takes_no_extra_slot_after_its_send():
    """The move's own send marks the frame buried by its new id; that must not cost a
    further frame push (and its pacer slot)."""

    async def scenario():
        events = []
        bot = _logging_mock_bot(events)
        t = Transport(bot=bot)
        _record_pacer(t, events)
        t.show_bottom_frame(1, 7, "panel", "rows")
        await t.flush_frames()
        await t.send(1, "later", thread_id=7)  # ids keep growing, as in Telegram
        await t.flush_frames()
        moved = events[[name for name, _ in events].index("delete") - 2 :]
        assert [name for name, _ in moved] == ["take_spare", "take_spare_now", "delete", "send"]

    asyncio.run(scenario())


# ── A burst below 2 slots ───────────────────────────────────────────


def test_a_transport_with_burst_one_still_moves_the_panel():
    async def scenario():
        bot = _Bot()
        t = Transport(bot=bot, pace_per_min=600, pace_burst=1)  # one slot per 0.1 s
        t.show_bottom_frame(1, 7, "panel", "rows")
        await t.flush_frames()
        first = t.bottom_frame_id("panel")
        t._pacer.tokens = 1.0
        await t.send(1, "later", thread_id=7)
        await asyncio.wait_for(t.flush_frames(), 2)
        assert [m for _at, m, _p in bot.calls] == ["send", "send", "delete", "send"]
        assert bot.texts("delete") == [first] and bot.texts("send")[-1] == "rows"
        deleted_at, sent_at = [at for at, m, _p in bot.calls][-2:]
        assert 0.08 <= sent_at - deleted_at < 0.2  # one slot between the two calls

    asyncio.run(scenario())


def test_content_recorded_during_the_moves_delete_rides_its_send():
    async def scenario():
        bot = MockBot()
        real_delete = bot.delete_message
        t = Transport(bot=bot)

        async def delete_message(chat_id, message_id):
            t.show_bottom_frame(1, 7, "panel", "rows 1s")  # the clock ticks mid-delete
            return await real_delete(chat_id, message_id)

        t.show_bottom_frame(1, 7, "panel", "rows 0s")
        await t.flush_frames()
        bot.delete_message = delete_message
        await t.send(1, "later", thread_id=7)
        await asyncio.wait_for(t.flush_frames(), 2)
        assert bot.created[-1][2] == "rows 1s"
        assert bot.edits == []  # no follow-up edit

    asyncio.run(scenario())


def test_a_finalizing_edit_recorded_while_a_move_waits_for_its_second_slot_goes_at_once():
    async def scenario():
        bot = MockBot()
        t = Transport(bot=bot, pace_per_min=60, pace_burst=3)  # one slot per second
        card = await t.send(1, "card", thread_id=7)
        t.show_bottom_frame(1, 7, "panel", "rows")
        await t.flush_frames()
        t._pacer.tokens = 0.0
        t.note_message(7, t.bottom_frame_id("panel") + 1)  # a move, short of its pair
        await asyncio.sleep(1.1)  # one slot came back; the move handed it back and waits
        started = time.monotonic()
        assert await asyncio.wait_for(t.land_frame(1, card, "final", thread_id=7), 5)
        assert time.monotonic() - started < 0.3

    asyncio.run(scenario())


def test_the_moves_send_waits_out_a_flood_recorded_during_its_delete():
    async def scenario():
        bot = MockBot()
        t = Transport(bot=bot)
        t.show_bottom_frame(1, 7, "panel", "rows")
        await t.flush_frames()
        real_send, real_delete = bot.send_message, bot.delete_message
        flood = {}
        panel_sends = []

        async def delete_message(chat_id, message_id):
            result = await real_delete(chat_id, message_id)
            flood["until"] = t.flood_until = time.monotonic() + 0.3  # another call's 429
            return result

        async def send_message(chat_id, text, **kw):
            if text == "rows":
                panel_sends.append(time.monotonic())
            return await real_send(chat_id, text, **kw)

        bot.delete_message, bot.send_message = delete_message, send_message
        await t.send(1, "later", thread_id=7)
        await asyncio.wait_for(t.flush_frames(), 2)
        assert panel_sends and panel_sends[-1] >= flood["until"]
        assert bot.peak_on_screen(_is_panel) == 1

    asyncio.run(scenario())
