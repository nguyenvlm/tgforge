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
from tgforge.plugins import localfs
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


def _view_text(c, inst) -> str:
    """The Files view's newest landed text (its bottom message, sent or edited)."""
    view_id = inst.bottom_frame_id(localfs.VIEW_FRAME)
    edited = [text for mid, text in c.bot.edits if mid == view_id]
    return edited[-1] if edited else next(t for m, _th, t in c.bot.created if m == view_id)


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


def _record_views(t):
    """Spy the window's live frames: (time, plain view) per record."""
    views = []
    real = t.show_frame

    def spy(msg_id, plain, md=None, reply_markup=None):
        views.append((time.monotonic(), plain))
        real(msg_id, plain, md, reply_markup=reply_markup)

    t.show_frame = spy
    return views


def test_the_one_shot_pump_records_at_most_one_frame_per_interval(tmp_path, monkeypatch):
    monkeypatch.setattr(drv, "EDIT_INTERVAL", 0.4)

    async def scenario():
        c = client(tmp_path)
        t = window(c, tmp_path)
        views = _record_views(t)
        script = "for i in $(seq 1 20); do echo line$i; sleep 0.06; done"
        await c.send(f"!{script}", thread_id=THREAD)
        await asyncio.wait_for(c.settle(), 10)
        times = [at for at, _view in views]
        assert 2 <= len(times) <= 5  # ~1.2 s of output: not one frame per chunk (20)
        assert all(b - a >= 0.4 - 0.02 for a, b in zip(times, times[1:], strict=False))

    asyncio.run(scenario())


def test_output_before_a_quiet_stretch_shows_within_one_interval(tmp_path, monkeypatch):
    """`b` lands 0.3 s after `a` (shown at once), inside the 1 s interval, then the
    command goes quiet for 2 s: a timer shows `b` when the interval ends."""
    monkeypatch.setattr(drv, "EDIT_INTERVAL", 1.0)

    async def scenario():
        c = client(tmp_path)
        t = window(c, tmp_path)
        views = _record_views(t)
        started = time.monotonic()
        await c.send("!printf a; sleep 0.3; printf b; sleep 2", thread_id=THREAD)
        await asyncio.wait_for(c.settle(), 10)
        shown = [at - started for at, view in views if "b" in view.split("\n", 1)[1]]
        assert shown and shown[0] < 1.6  # at the interval's end, not at the 2.3 s exit

    asyncio.run(scenario())


def test_a_cut_off_utf8_character_at_the_end_is_kept(tmp_path):
    async def scenario():
        c = client(tmp_path)
        window(c, tmp_path)
        await c.send("!printf 'ok\\342\\202'", thread_id=THREAD)  # 2 of €'s 3 bytes
        await asyncio.wait_for(c.settle(), 10)
        assert "ok�" in c.edits[-1]  # flushed as a replacement, not dropped

    asyncio.run(scenario())


def test_a_typed_path_jumps_to_that_folder(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(kernel, "TITLE_MIN_INTERVAL", 0)

    async def scenario():
        c = TestClient(Localfs(), home=str(tmp_path))
        root = _nested(tmp_path)
        inst = await _files_window(c, root)
        cases = [
            (str(root / "d0" / "d1"), root / "d0" / "d1"),  # absolute
            ("d2/d3", root / "d0" / "d1" / "d2" / "d3"),  # relative to the current folder
            ("..", root / "d0" / "d1" / "d2"),
            ("~/root", root),  # ~ expanded
        ]
        for typed, expected in cases:
            sent_before = len(c.bot.sent)
            await c.send(typed, thread_id=inst.thread_id)
            await c.settle()
            await c.core.flush_frames()
            typed_id = next(mid for mid, _t, text in reversed(c.bot.created) if text == typed)
            assert inst.saved["cwd"] == str(expected), typed
            assert _view_text(c, inst).startswith(f"📂 {expected}"), typed  # the view shows it
            assert all(text.startswith("📂") for _t, text in c.bot.sent[sent_before:])  # no reply
            assert typed_id in c.bot.deleted  # the typed path goes

    asyncio.run(scenario())


def test_a_bad_typed_path_gets_a_brief_notice_deleted_later(tmp_path, monkeypatch):
    monkeypatch.setattr(localfs, "NOTE_LIFETIME", 0.2)

    async def scenario():
        c = TestClient(Localfs(), home=str(tmp_path))
        root = _nested(tmp_path)
        (root / "a.txt").write_text("x")
        inst = await _files_window(c, root)
        for typed, reason in [("nope", "no such folder"), ("a.txt", "not a folder")]:
            await c.send(typed, thread_id=inst.thread_id)
            await c.settle()
            typed_id = next(mid for mid, _t, text in c.bot.created if text == typed)
            note_id, note = next(
                (mid, text) for mid, _t, text in reversed(c.bot.created) if text.startswith("⚠️")
            )
            assert note == f"⚠️ {reason}: {typed}"
            assert inst.saved["cwd"] == str(root)  # stayed put
            assert typed_id in c.bot.deleted
            assert note_id not in c.bot.deleted  # still showing
            await asyncio.sleep(0.3)
            assert note_id in c.bot.deleted  # gone after NOTE_LIFETIME

    asyncio.run(scenario())


def test_a_typed_path_that_cannot_be_checked_gets_a_notice(tmp_path):
    async def scenario():
        c = TestClient(Localfs(), home=str(tmp_path))
        root = _nested(tmp_path)
        inst = await _files_window(c, root)
        await c.send("x" * 5000, thread_id=inst.thread_id)  # longer than any path
        await c.settle()
        assert any(text.startswith("⚠️ ") for _t, text in c.bot.sent)
        assert inst.saved["cwd"] == str(root)

    asyncio.run(scenario())


def test_folder_clicks_and_typed_paths_make_no_rename(tmp_path, monkeypatch):
    monkeypatch.setattr(kernel, "TITLE_MIN_INTERVAL", 0)

    async def scenario():
        c = TestClient(Localfs(), home=str(tmp_path))
        root = _nested(tmp_path)
        inst = await _files_window(c, root)
        renames = _flood_once(c.bot, "edit_forum_topic", 0, times=0)
        for _ in range(3):
            await _click(c, inst, "cd:0")
        await c.send(str(root), thread_id=inst.thread_id)
        await c.settle()
        await c.core.flush_frames()
        assert renames["calls"] == []

    asyncio.run(scenario())


def test_the_files_view_stays_the_bottom_message(tmp_path, monkeypatch):
    monkeypatch.setattr(localfs, "NOTE_LIFETIME", 0.2)

    async def scenario():
        c = TestClient(Localfs(), home=str(tmp_path))
        root = _nested(tmp_path)
        inst = await _files_window(c, root)
        view_id = inst.bottom_frame_id(localfs.VIEW_FRAME)
        await _click(c, inst, "cd:0")
        await c.core.flush_frames()
        assert inst.bottom_frame_id(localfs.VIEW_FRAME) == view_id  # a tap edits in place
        assert c.edits[-1].startswith(f"📂 {root}/d0")

        await inst.send("⭐ bookmarked d0")  # a new message below the view
        await c.core.flush_frames()
        new_id = inst.bottom_frame_id(localfs.VIEW_FRAME)
        assert new_id > c.bot.created[-2][0]  # re-sent below it
        assert view_id in c.bot.deleted  # the old copy is gone
        assert c.bot.last_markup is not None  # with its buttons

        views_before = sum(text.startswith("📂") for _m, _t, text in c.bot.created)
        await c.send("nope", thread_id=inst.thread_id)  # a bad path: typed message + notice
        await c.settle()
        await c.core.flush_frames()
        views_after = sum(text.startswith("📂") for _m, _t, text in c.bot.created)
        assert views_after - views_before == 1  # one re-send for the typed message, none more

    asyncio.run(scenario())


def test_the_files_view_moves_as_one_copy_below_a_burst_of_messages(tmp_path):
    async def scenario():
        c = TestClient(Localfs(), home=str(tmp_path))
        c.core._pacer = kernel._Pacer(1200, kernel.PACE_BURST)  # fast, live burst size
        inst = await _files_window(c, _nested(tmp_path))
        for n in range(3):
            await inst.send(f"note {n}")  # each buries the view
            await asyncio.sleep(0.05)
        await c.core.flush_frames()
        is_view = lambda text: text.startswith("📂")  # noqa: E731
        assert c.bot.peak_on_screen(is_view) == 1
        view_id = inst.bottom_frame_id(localfs.VIEW_FRAME)
        assert c.bot.created[-1][0] == view_id  # at the bottom
        log = c.bot.screen_log
        moves = [at for at, (kind, _m, _t) in enumerate(log) if kind == "delete"]
        assert moves, "the view never moved"
        for at in moves:  # each old copy's delete is the call right before its re-send
            kind, _new, text = log[at + 1]
            assert kind == "create" and is_view(text)

    asyncio.run(scenario())
