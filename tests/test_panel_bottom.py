"""The background-jobs panel is its own message, kept by the kernel as the window's
bottom-most (a bottom frame): any newer message in the thread — sent, received, or a
topic rename's service message — re-sends it below, independent of the heartbeat, the
between-turn updater, or the CLI process. See AGENTS.md → Message-ordering invariant."""

from __future__ import annotations

import asyncio
import time

from aiogram.exceptions import TelegramRetryAfter
from claude_fakes import FakeProc, cancel_task, client, drain, result, screen, window

from tgforge.plugins.claude import background
from tgforge.plugins.claude import driver as drv


class _Flood(TelegramRetryAfter):
    def __init__(self, retry_after):
        self.retry_after = retry_after


def _with_job(c, tmp_path, done=None):
    t = window(c, tmp_path)
    out = tmp_path / "job.output"
    out.write_text("working\n")
    now = time.monotonic()
    t.background_tasks = {
        "b1": {"path": str(out), "label": "a job", "start": now - 5, "done": done, "done_at": now}
    }
    return t


def _panel_id(t):
    return t.bottom_frame_id(drv.PANEL_FRAME)


def _bottom(c):
    return screen(c)[-1][0]


def test_a_later_message_re_anchors_the_panel_after_the_cli_process_ended(tmp_path):
    """No heartbeat, no updater, no CLI process: the kernel alone moves the panel."""

    async def scenario():
        c = client(tmp_path)
        t = _with_job(c, tmp_path, done="✓")  # a finished row still lingering
        t._paint_panel(0)
        t._open_holder()
        await drain(c, t)
        t.proc = FakeProc([result("done")])
        t.pending_writes = []
        await asyncio.wait_for(t._reader(), 5)  # idle → the CLI process is ended
        await drain(c, t)
        await cancel_task(t.background_updater_task)
        assert t.proc is None and t.heartbeat_task is None

        reply = await t.send("a command reply, after the turn")
        await drain(c, t)
        assert _panel_id(t) > reply and _bottom(c) == _panel_id(t)

    asyncio.run(scenario())


def test_the_cli_process_ending_leaves_the_updater_running(tmp_path):
    async def scenario():
        c = client(tmp_path)
        t = _with_job(c, tmp_path, done="✓")
        t._ensure_background_updater()
        t.proc = FakeProc([])  # the process ends with no turn to settle
        await asyncio.wait_for(t._reader(), 5)
        assert t.background_updater_task is not None  # the panel's clock keeps ticking
        await cancel_task(t.background_updater_task)

    asyncio.run(scenario())


def test_a_mid_turn_message_re_anchors_without_a_heartbeat_paint(tmp_path):
    async def scenario():
        c = client(tmp_path)
        t = _with_job(c, tmp_path)
        t._paint_panel(0)
        t._open_holder()
        await drain(c, t)
        await t._stop_heartbeat()  # no painter tick at all
        first = _panel_id(t)
        incoming = c.bot._id()
        c.bot.created.append((incoming, 555, "the user's message mid-turn"))
        c.core.note_message(555, incoming)  # as handle_message records an inbound one
        await drain(c, t)
        assert _panel_id(t) > incoming and first in c.bot.deleted

    asyncio.run(scenario())


def test_a_dropped_re_send_is_retried(tmp_path):
    async def scenario():
        c = client(tmp_path)
        t = _with_job(c, tmp_path)
        t._paint_panel(0)
        await drain(c, t)
        real_send = c.bot.send_message
        floods = {"n": 0}

        async def send(chat_id, text, **kw):
            if "background ·" in text and floods["n"] == 0:
                floods["n"] += 1
                raise _Flood(0.05)
            return await real_send(chat_id, text, **kw)

        c.bot.send_message = send
        reply = await t.send("a later message")
        await drain(c, t)
        assert floods["n"] == 1 and _panel_id(t) > reply

    asyncio.run(scenario())


def test_a_topic_rename_re_anchors_the_panel(tmp_path):
    async def scenario():
        c = client(tmp_path)
        t = _with_job(c, tmp_path)
        t._paint_panel(0)
        await drain(c, t)
        first = _panel_id(t)
        await t.rename("a new title")
        await drain(c, t)
        assert _panel_id(t) != first and first in c.bot.deleted

    asyncio.run(scenario())


def test_panel_sends_are_silent_and_the_answer_carries_no_panel(tmp_path):
    async def scenario():
        c = client(tmp_path)
        t = _with_job(c, tmp_path)
        t._paint_panel(0)
        t._open_holder()
        await drain(c, t)
        await t._finalize_turn("the answer")
        await drain(c, t)
        panel = _panel_id(t)
        assert panel in c.bot.silent
        assert _bottom(c) == panel
        answer = next(mid for mid, text in screen(c) if text == "the answer")
        assert answer not in c.bot.silent
        assert all("background ·" not in text for mid, text in screen(c) if mid != panel)

    asyncio.run(scenario())


def test_the_updater_retires_the_panel_once_the_last_row_lingers_out(tmp_path, monkeypatch):
    async def scenario():
        monkeypatch.setattr(drv, "UPDATER_INTERVAL", 0.01)
        c = client(tmp_path)
        t = _with_job(c, tmp_path, done="✓")
        t._ensure_background_updater()
        await asyncio.sleep(0.02)  # the updater's first tick
        await drain(c, t)
        panel = _panel_id(t)
        assert panel is not None
        t.background_tasks["b1"]["done_at"] = time.monotonic() - background.FINISHED_LINGER - 1
        await asyncio.wait_for(t.background_updater_task, 2)
        await drain(c, t)
        assert panel in c.bot.deleted and _panel_id(t) is None

    asyncio.run(scenario())


def test_a_registered_job_starts_the_updater_mid_turn(tmp_path):
    async def scenario():
        c = client(tmp_path)
        t = window(c, tmp_path)
        t._open_holder()
        await drain(c, t)
        out = tmp_path / "tasks" / "abc.output"
        out.parent.mkdir()
        out.write_text("")
        ev = {
            "type": "user",
            "tool_use_result": {"backgroundTaskId": "abc"},
            "message": {
                "content": [{"type": "tool_result", "tool_use_id": "t1", "content": f"see {out}"}]
            },
        }
        from claude_fakes import line

        hold = asyncio.Event()
        t.proc = FakeProc([line(ev), hold])
        t.pending_writes = []
        reader = asyncio.create_task(t._reader())
        await asyncio.sleep(0.05)
        await drain(c, t)
        assert t.background_updater_task is not None
        assert "background · 1 job" in dict(screen(c))[_panel_id(t)]
        hold.set()
        await asyncio.wait_for(reader, 5)
        await cancel_task(t.background_updater_task)

    asyncio.run(scenario())
