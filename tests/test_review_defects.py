"""Defects from the review: a session/workspace switch drops an idle CLI process, the
model-list reset restores the app's list, every window subprocess is reaped with the
window, the shell offers the agent's workspace roots, and the example config names a
real bot home."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from claude_fakes import THREAD, client, window

from tgforge.plugins.claude.config import write_roots
from tgforge.plugins.gcloud import Gcloud
from tgforge.plugins.shell import Shell, ShellTopic
from tgforge.testing import TestClient

REPO = Path(__file__).resolve().parent.parent


def _idle_proc(t):
    killed = []
    t.proc = SimpleNamespace(returncode=None, kill=lambda: killed.append(True))
    t.busy = False
    return killed


def test_clear_drops_a_live_idle_cli_process(tmp_path):
    async def scenario():
        c = client(tmp_path)
        t = window(c, tmp_path)
        killed = _idle_proc(t)  # kept alive after a turn with background jobs
        old = t.session_id
        await t.clear(None)
        assert t.session_id != old
        assert killed == [True]  # the next prompt spawns against the new session

    asyncio.run(scenario())


def test_a_workspace_switch_drops_a_live_idle_cli_process(tmp_path):
    async def scenario():
        c = client(tmp_path)
        a, b = tmp_path / "a", tmp_path / "b"
        a.mkdir()
        b.mkdir()
        c.core.plugin_by_id["claude"].workspaces = [a, b]
        t = window(c, tmp_path)
        t.workspace = str(a)
        killed = _idle_proc(t)
        task = asyncio.create_task(t._switch_workspace())
        await c.tap("b")
        await asyncio.wait_for(task, 2)
        assert t.workspace == str(b)
        assert killed == [True]  # the old cwd's process is not reused

    asyncio.run(scenario())


def test_login_spawns_through_the_window(tmp_path):
    async def scenario():
        c = client(tmp_path)
        t = window(c, tmp_path)
        plugin = c.core.plugin_by_id["claude"]
        cli = tmp_path / "fakelogin"
        cli.write_text("#!/bin/sh\necho 'Open https://example.test/login'\nsleep 30\n")
        cli.chmod(0o755)
        plugin.claude_bin = str(cli)
        seen = {}

        async def ask_text(prompt, timeout=180):
            seen["live"] = [p for p in t._procs if p.returncode is None]
            await c.core.close_window(THREAD)  # the window closes mid-login
            return None

        t.ask_text = ask_text
        assert await asyncio.wait_for(plugin._run_login(t, "work", tmp_path / "cfg"), 5) is False
        assert seen["live"]  # the login child was the window's
        assert all(p.returncode is not None for p in seen["live"])  # and reaped with it

    asyncio.run(scenario())


def test_gcloud_spawns_through_the_window(tmp_path):
    async def scenario():
        c = TestClient(Gcloud(), home=str(tmp_path))
        inst = await c.core.open_window(c.core.chat_id, ShellTopic, "w")
        spawned = []
        real_spawn = inst.spawn

        async def spawn(*args, **kwargs):
            spawned.append(args)
            return await real_spawn("/bin/echo", "no url here", **kwargs)

        inst.spawn = spawn
        ctx = c.core._ctx("gcloud", None, c.core.chat_id, inst.thread_id, "", "adc", inst)
        await c.core.plugin_by_id["gcloud"].gcloud(ctx)
        assert spawned and "gcloud" in spawned[0] and "--update-adc" in spawned[0]
        await inst._kill()

    asyncio.run(scenario())


def test_the_shell_offers_the_agents_workspace_roots(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    write_roots(tmp_path, [str(root)])  # what the wizard / `tgforge workspaces` write
    alone = TestClient(Shell(), home=str(tmp_path))  # no claude plugin in the app
    assert alone.core.plugin_by_id["shell"].workspace_roots(alone.core) == [root.resolve()]
    write_roots(tmp_path, [])
    own = TestClient(Shell(roots=[str(root)]), home=str(tmp_path))
    assert own.core.plugin_by_id["shell"].workspace_roots(own.core) == [root.resolve()]


def test_the_shell_offers_a_configured_claude_plugins_roots(tmp_path):
    from tgforge.plugins.claude import Claude

    root = tmp_path / "project"
    root.mkdir()
    c = TestClient(Claude(roots=[str(root)]), Shell(), home=str(tmp_path))  # nothing saved
    assert c.core.plugin_by_id["shell"].workspace_roots(c.core) == [root.resolve()]


def test_the_shell_window_starts_in_the_agents_root(tmp_path):
    async def scenario():
        root = tmp_path / "project"
        root.mkdir()
        write_roots(tmp_path, [str(root)])
        c = TestClient(Shell(), home=str(tmp_path))
        inst = await c.core.open_window(c.core.chat_id, ShellTopic, "w")
        try:
            assert inst.saved["cwd"] == str(root.resolve())
        finally:
            await inst._kill()

    asyncio.run(scenario())


def test_the_example_config_names_a_bot_home():
    example = json.loads((REPO / "bot.json.example").read_text())
    assert example["home"] == "~/.tgforge/mybot"  # never the parent the CLI scans


def test_a_failed_launch_is_reported(tmp_path):
    async def scenario():
        c = client(tmp_path)
        t = window(c, tmp_path)

        async def broken():
            raise OSError("no claude binary")

        t._ensure_proc = broken
        await t.submit("hello")
        await t._sent()
        assert "failed to launch: no claude binary" in c.replies

    asyncio.run(scenario())
