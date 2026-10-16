"""Every plugin's live message goes through the kernel's frame-skip output: a handler or
a loop reading local input records the newest content and returns at once, so a
Telegram flood-wait never stalls it (AGENTS.md → Frame-skip output)."""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

from aiogram.exceptions import TelegramRetryAfter
from claude_fakes import THREAD, client, window

from tgforge.base import kernel
from tgforge.plugins.claude import driver as drv
from tgforge.plugins.localfs import Localfs, LocalfsTopic
from tgforge.testing import TestClient


class _Flood(TelegramRetryAfter):
    def __init__(self, retry_after):
        self.retry_after = retry_after


def _flood_once(bot, method: str, retry_after: float, times: int = 1):
    real = getattr(bot, method)
    state = {"left": times, "calls": []}

    async def call(*args, **kwargs):
        state["calls"].append(kwargs.get("name") or kwargs.get("text") or (args[0] if args else ""))
        if state["left"] > 0:
            state["left"] -= 1
            raise _Flood(retry_after)
        return await real(*args, **kwargs)

    setattr(bot, method, call)
    return state


def test_the_one_shot_pump_keeps_reading_through_a_flood(tmp_path, monkeypatch):
    """The command prints while the output message is flood-waited; the pump never
    waits on Telegram, so the command finishes well inside SHELL_TIMEOUT."""
    monkeypatch.setattr(drv, "SHELL_TIMEOUT", 1.5)

    async def scenario():
        c = client(tmp_path)
        window(c, tmp_path)
        _flood_once(c.bot, "edit_message_text", 2.5)
        script = "for i in $(seq 1 40); do echo line$i; sleep 0.01; done"
        await c.send(f"!{script}", thread_id=THREAD)
        await asyncio.wait_for(c.settle(), 10)
        final = c.edits[-1]
        assert "exit 0" in final and "line40" in final  # finished on time, not timed out
        assert "timed out" not in " ".join(c.edits)

    asyncio.run(scenario())


def test_a_one_shot_window_close_reaps_the_command(tmp_path):
    async def scenario():
        c = client(tmp_path)
        t = window(c, tmp_path)
        await c.send("!sleep 30", thread_id=THREAD)
        for _ in range(100):
            if any(p.returncode is None for p in t._procs):
                break
            await asyncio.sleep(0.02)
        proc = next(iter(t._procs))
        await c.core.close_window(THREAD)
        assert proc.returncode is not None  # the window's close reaped it
        await asyncio.wait_for(c.settle(), 5)

    asyncio.run(scenario())


def _nested(tmp_path, depth=5):
    path = tmp_path / "root"
    for i in range(depth):
        path = path / f"d{i}"
    path.mkdir(parents=True)
    return tmp_path / "root"


async def _files_window(c, root):
    inst = await c.core.open_window(c.core.chat_id, LocalfsTopic, "browse")
    inst.saved["cwd"] = str(root)
    inst._redraw()
    await c.core.flush_frames()
    return inst


async def _click(c, inst, arg):
    """Run the window's button handler for a tap; returns how long it took."""
    started = time.monotonic()
    await inst.button(None, arg)
    return time.monotonic() - started


def test_files_clicks_during_a_flood_return_at_once_and_show_the_last_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(kernel, "TITLE_MIN_INTERVAL", 0)

    async def scenario():
        c = TestClient(Localfs(), home=str(tmp_path))
        root = _nested(tmp_path)
        inst = await _files_window(c, root)
        flood = _flood_once(c.bot, "edit_message_text", 0.3)
        took = [await _click(c, inst, "cd:0") for _ in range(5)]
        assert max(took) < 0.1  # each click returned without waiting on Telegram
        await c.core.flush_frames()
        assert c.edits[-1].startswith(f"📂 {root}/d0/d1/d2/d3/d4")
        assert len(flood["calls"]) == 2  # the flooded view, then the newest; 3 skipped

    asyncio.run(scenario())


def test_five_quick_folder_clicks_give_one_rename_to_the_last_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(kernel, "TITLE_MIN_INTERVAL", 0.3)

    async def scenario():
        c = TestClient(Localfs(), home=str(tmp_path))
        root = _nested(tmp_path)
        inst = await _files_window(c, root)
        await inst.refresh_title()
        await c.core.flush_frames()
        renames = _flood_once(c.bot, "edit_forum_topic", 0, times=0)
        for _ in range(5):
            await _click(c, inst, "cd:0")
            await asyncio.sleep(0.01)  # the frame task runs between clicks
        await c.core.flush_frames()
        assert renames["calls"] == ["📁 browse · d4"]

    asyncio.run(scenario())


def test_a_click_never_waits_on_a_flood_waited_rename(tmp_path, monkeypatch):
    monkeypatch.setattr(kernel, "TITLE_MIN_INTERVAL", 0)

    async def scenario():
        c = TestClient(Localfs(), home=str(tmp_path))
        root = _nested(tmp_path)
        inst = await _files_window(c, root)
        _flood_once(c.bot, "edit_forum_topic", 5)
        assert await _click(c, inst, "cd:0") < 0.1
        await asyncio.sleep(0.05)  # the rename hits its flood-wait meanwhile
        assert await _click(c, inst, "cd:0") < 0.1

    asyncio.run(scenario())


def test_an_explicit_rename_skips_the_rename_spacing(tmp_path, monkeypatch):
    monkeypatch.setattr(kernel, "TITLE_MIN_INTERVAL", 30)

    async def scenario():
        c = client(tmp_path)
        t = window(c, tmp_path)
        await t.refresh_title()
        await asyncio.wait_for(c.core.flush_frames(), 1)
        ctx = SimpleNamespace(args="My topic")
        await t.rename_cmd(ctx)
        await asyncio.wait_for(c.core.flush_frames(), 1)  # not held 30 s
        assert c.core._names()[THREAD] == "🤖 My topic"

    asyncio.run(scenario())


def test_a_chatty_one_shot_command_finishes_well_inside_the_timeout(tmp_path, monkeypatch):
    """200k short lines: the pump renders at most one frame per interval, so the
    command's own speed decides, not rendering on the event loop."""
    monkeypatch.setattr(drv, "SHELL_TIMEOUT", 5)

    async def scenario():
        c = client(tmp_path)
        window(c, tmp_path)
        started = time.monotonic()
        await c.send("!seq 1 200000", thread_id=THREAD)
        await asyncio.wait_for(c.settle(), 30)
        final = c.edits[-1]
        assert "exit 0" in final and "200000" in final
        assert time.monotonic() - started < 5

    asyncio.run(scenario())
