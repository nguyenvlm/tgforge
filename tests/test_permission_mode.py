"""The Claude permission mode is an app-wide setting: `auto` by default, overridable
at construction, changed via `set_permission_mode` (persisted), restored by `_load`,
shown in `/status`, and editable through the `/mode` menu. On spawn it becomes the
CLI `--permission-mode` flag, replacing the old `--allowedTools` allowlist."""

from __future__ import annotations

import asyncio
import stat
from types import SimpleNamespace

from tgforge.base.config import BotConfig
from tgforge.base.kernel import Kernel
from tgforge.plugins.claude import Claude, ClaudeTopic
from tgforge.testing import MockBot, TestClient

_FAKE_CLI = """#!/usr/bin/env python3
import sys, json
sys.stdin.readline()
print(json.dumps({"type": "result", "result": "ok",
      "subtype": "success", "is_error": False}), flush=True)
"""


def test_default_is_auto():
    assert Claude().permission_mode == "auto"


def test_constructor_override():
    assert Claude(permission_mode="plan").permission_mode == "plan"


def test_set_persists_and_reloads(tmp_path):
    c = TestClient(Claude(), home=str(tmp_path))
    plugin = c.core.plugin_by_id["claude"]
    plugin.set_permission_mode("bypassPermissions")
    assert plugin.saved["permission_mode"] == "bypassPermissions"
    plugin.permission_mode = "auto"  # scramble in-memory
    plugin._load()  # a fresh load restores the saved value
    assert plugin.permission_mode == "bypassPermissions"


def test_status_shows_mode(tmp_path):
    async def scenario():
        c = TestClient(Claude(), home=str(tmp_path))
        t = c.core._instantiate(ClaudeTopic, 555, "work")
        assert "mode auto" in await t.status(None)

    asyncio.run(scenario())


def test_mode_menu_changes_the_setting(tmp_path):
    async def scenario():
        c = TestClient(Claude(), home=str(tmp_path))
        t = c.core._instantiate(ClaudeTopic, 555, "work")
        c.core.owners[555] = "claude"
        task = asyncio.create_task(t.mode(None))
        await c.pump()
        assert "acceptEdits" in c.buttons
        await c.tap("acceptEdits")
        await asyncio.wait_for(task, 2)
        assert t.plugin.permission_mode == "acceptEdits"
        assert any("permission mode → acceptEdits" in text for text in c.replies)

    asyncio.run(scenario())


def test_mode_change_kills_an_idle_proc(tmp_path):
    async def scenario():
        c = TestClient(Claude(), home=str(tmp_path))
        t = c.core._instantiate(ClaudeTopic, 555, "work")
        c.core.owners[555] = "claude"
        killed = []
        t.proc = SimpleNamespace(returncode=None, kill=lambda: killed.append(True))
        t.busy = False
        task = asyncio.create_task(t.mode(None))
        await c.pump()
        await c.tap("plan")  # a different mode → respawn needed
        await asyncio.wait_for(task, 2)
        assert killed == [True]  # idle proc killed so the next message respawns

    asyncio.run(scenario())


def test_mode_unchanged_leaves_proc_alone(tmp_path):
    async def scenario():
        c = TestClient(Claude(), home=str(tmp_path))
        t = c.core._instantiate(ClaudeTopic, 555, "work")
        c.core.owners[555] = "claude"
        killed = []
        t.proc = SimpleNamespace(returncode=None, kill=lambda: killed.append(True))
        t.busy = False
        task = asyncio.create_task(t.mode(None))
        await c.pump()
        await c.tap("auto ✓")  # the current mode → no-op
        await asyncio.wait_for(task, 2)
        assert killed == []  # nothing changed, proc untouched

    asyncio.run(scenario())


def test_spawn_uses_permission_mode_not_allowed_tools(tmp_path, monkeypatch):
    import tgforge.plugins.claude.driver as drv

    fake = tmp_path / "fakeclaude"
    fake.write_text(_FAKE_CLI)
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    captured = {}
    real = drv.asyncio.create_subprocess_exec

    async def spy(*cmd, **kw):
        captured["cmd"] = list(cmd)
        return await real(*cmd, **kw)

    monkeypatch.setattr(drv.asyncio, "create_subprocess_exec", spy)

    async def scenario():
        cfg = BotConfig(token="x", home=str(tmp_path), owner_id=1, chat_id=100, bot_username="bot")
        core = Kernel(MockBot(), cfg, [Claude()])
        claude = core.plugin_by_id["claude"]
        claude.claude_bin = str(fake)
        claude.claude_dir = tmp_path / ".claude"
        t = core._instantiate(ClaudeTopic, 555, "work")
        t.workspace = str(tmp_path)
        await t.submit("hi")
        for _ in range(60):
            if "cmd" in captured:
                break
            await asyncio.sleep(0.1)
        proc = t.proc
        await t.on_close()
        if proc is not None:
            await proc.wait()  # reap the fake CLI so its transport closes cleanly

    asyncio.run(scenario())
    assert "--permission-mode" in captured["cmd"]
    assert "auto" in captured["cmd"]
    assert "--allowedTools" not in captured["cmd"]
