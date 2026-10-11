"""A bot turn runs under the workspace's system_prompt.txt: the nearest one at or
above the workspace becomes the CLI `--system-prompt-file` flag, the same lookup
the interactive shell wrapper does; no file means no flag."""

from __future__ import annotations

import asyncio
import stat

from tgforge.base.config import BotConfig
from tgforge.base.kernel import Kernel
from tgforge.plugins.claude import Claude, ClaudeTopic
from tgforge.plugins.claude.config import system_prompt_file
from tgforge.testing import MockBot

_FAKE_CLI = """#!/usr/bin/env python3
import sys, json
sys.stdin.readline()
print(json.dumps({"type": "result", "result": "ok",
      "subtype": "success", "is_error": False}), flush=True)
"""


def test_lookup_finds_file_in_workspace(tmp_path):
    (tmp_path / "system_prompt.txt").write_text("rules")
    assert system_prompt_file(tmp_path) == tmp_path / "system_prompt.txt"


def test_lookup_walks_up_to_a_parent(tmp_path):
    (tmp_path / "system_prompt.txt").write_text("rules")
    child = tmp_path / "repo" / "sub"
    child.mkdir(parents=True)
    assert system_prompt_file(child) == tmp_path / "system_prompt.txt"


def test_lookup_nearest_wins(tmp_path):
    (tmp_path / "system_prompt.txt").write_text("outer")
    inner = tmp_path / "inner"
    inner.mkdir()
    (inner / "system_prompt.txt").write_text("inner")
    assert system_prompt_file(inner) == inner / "system_prompt.txt"


def test_lookup_ignores_a_directory_named_like_the_file(tmp_path):
    (tmp_path / "system_prompt.txt").mkdir()
    assert system_prompt_file(tmp_path) is None


def _spawned_cmd(tmp_path, monkeypatch, workspace) -> list[str]:
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
        topic = core._instantiate(ClaudeTopic, 555, "work")
        topic.workspace = str(workspace)
        await topic.submit("hi")
        for _ in range(60):
            if "cmd" in captured:
                break
            await asyncio.sleep(0.1)
        proc = topic.proc
        await topic.on_close()
        if proc is not None:
            await proc.wait()

    asyncio.run(scenario())
    return captured["cmd"]


def test_spawn_passes_the_workspace_system_prompt(tmp_path, monkeypatch):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "system_prompt.txt").write_text("rules")
    cmd = _spawned_cmd(tmp_path, monkeypatch, workspace)
    flag = cmd.index("--system-prompt-file")
    assert cmd[flag + 1] == str(workspace / "system_prompt.txt")


def test_spawn_without_a_system_prompt_has_no_flag(tmp_path, monkeypatch):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    assert system_prompt_file(workspace) is None
    cmd = _spawned_cmd(tmp_path, monkeypatch, workspace)
    assert "--system-prompt-file" not in cmd
