"""Regression tests for the review fixes: prompt-helper bail on a failed send (#3),
media-only not cancelling a text prompt (#11), title dedup surviving refresh (#13),
concurrent-menu supersede (#15), close kept on a failed delete (#10), and an account
switch carrying its transcript (#2)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from aiogram.exceptions import TelegramAPIError

from tgforge.plugins.claude import Claude, ClaudeTopic
from tgforge.plugins.claude.driver import _project_dir
from tgforge.plugins.localfs import Localfs, LocalfsTopic
from tgforge.testing import TestClient


class _ApiErr(TelegramAPIError):
    def __init__(self):
        pass  # a bare API error; _call swallows it → None


# ── #3: prompt helpers bail when the prompt never rendered ─────────


def test_ask_buttons_bails_on_failed_send(tmp_path):
    async def scenario():
        c = TestClient(home=str(tmp_path))

        async def boom(*a, **k):
            raise _ApiErr()

        c.bot.send_message = boom
        value, mid = await asyncio.wait_for(c.core.ask_buttons(100, None, "Pick?", [("A", "a")]), 2)
        assert value is None and mid is None  # returned at once, not after the timeout

    asyncio.run(scenario())


def test_ask_text_bails_on_failed_prompt_send(tmp_path):
    async def scenario():
        c = TestClient(home=str(tmp_path))

        async def boom(*a, **k):
            raise _ApiErr()

        c.bot.send_message = boom
        assert await asyncio.wait_for(c.core.ask_text(100, None, "Path?"), 2) is None
        assert c.core._await_input.get(0) is None  # the feeder was cleaned up

    asyncio.run(scenario())


# ── #11: a media-only message must not cancel a pending ask_text ───


def test_media_only_does_not_cancel_ask_text(tmp_path):
    async def scenario():
        c = TestClient(home=str(tmp_path))
        task = asyncio.create_task(c.core.ask_text(100, None, "Path?"))
        await c.pump()
        photo = SimpleNamespace(
            text=None,
            caption=None,
            photo=["file"],
            document=None,
            from_user=SimpleNamespace(id=1),
            chat=SimpleNamespace(id=100),
            message_thread_id=None,
        )
        await c.core.handle_message(photo)
        assert not task.done()  # the prompt is still waiting, not cancelled
        await c.send("typed/path")
        assert await asyncio.wait_for(task, 2) == "typed/path"

    asyncio.run(scenario())


# ── #13: the deduped base ("#2") survives a title refresh ──────────


def test_dedup_base_survives_title_refresh(tmp_path):
    async def scenario():
        c = TestClient(Localfs(), home=str(tmp_path))
        w = await c.core.open_window(100, LocalfsTopic, None)
        w.saved["base_name"] = "Files #2"  # a deduped window
        w.saved["cwd"] = "/srv/proj"
        await w.refresh_title()
        # refresh recomposes from the stored base, not the class label → "#2" survives
        assert c.core._names()[w.thread_id] == "📁 Files #2 · proj"

    asyncio.run(scenario())


# ── #15: a second menu supersedes the first, no shared-stack mess ──


def test_second_menu_supersedes_the_first(tmp_path):
    async def scenario():
        c = TestClient(home=str(tmp_path))
        t1 = asyncio.create_task(c.core.menu(100, None, "M1", [("A", "a"), ("B", "b")]))
        await c.pump()
        t2 = asyncio.create_task(c.core.menu(100, None, "M2", [("X", "x"), ("Y", "y")]))
        await c.pump()
        assert await asyncio.wait_for(t1, 2) is None  # the first was dismissed
        await c.tap("X")
        assert await asyncio.wait_for(t2, 2) == "x"  # the second is the live one

    asyncio.run(scenario())


# ── #10: a failed topic delete keeps the window owned ──────────────


def test_close_keeps_window_when_delete_fails(tmp_path):
    async def scenario():
        c = TestClient(Localfs(), home=str(tmp_path))
        inst = await c.core.open_window(100, LocalfsTopic, None)
        tid = inst.thread_id

        async def boom(**k):
            raise _ApiErr()

        c.bot.delete_forum_topic = boom
        await c.core.close_window(tid)
        assert tid in c.core.owners  # not forgotten → reconcile can still handle it

    asyncio.run(scenario())


# ── #2: switching account carries the session transcript ──────────


def test_account_switch_carries_transcript(tmp_path):
    async def scenario():
        c = TestClient(Claude(), home=str(tmp_path))
        t = c.core._instantiate(ClaudeTopic, 555, "work")
        t.plugin.claude_dir = tmp_path / ".claude"
        t.workspace = str(tmp_path)
        old = _project_dir(t._session_config_dir(), Path(t.workspace)) / f"{t.session_id}.jsonl"
        old.parent.mkdir(parents=True, exist_ok=True)
        old.write_text('{"type":"user"}\n')
        new_dir = tmp_path / ".claude-work"
        t._switch_account(t._session_config_dir(), new_dir, str(new_dir))
        carried = _project_dir(new_dir, Path(t.workspace)) / f"{t.session_id}.jsonl"
        assert carried.exists()  # history followed the account, no fresh empty session

    asyncio.run(scenario())


# ── set_markup rides _call's factory contract (suggestion buttons) ─


def test_set_markup_attaches_keyboard(tmp_path):
    async def scenario():
        c = TestClient(Claude(), home=str(tmp_path))
        t = c.core._instantiate(ClaudeTopic, 555, "work")
        kb = await t._attach_suggestions(42, ["yes", "no"])
        assert kb is not None
        assert 42 in c.bot.markup_cleared  # the markup edit reached the (mock) API

    asyncio.run(scenario())
