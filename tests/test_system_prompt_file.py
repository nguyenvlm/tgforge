"""A bot turn runs under the workspace's system_prompt.txt: the nearest one at or
above the workspace becomes the CLI `--system-prompt-file` flag, the same lookup
the interactive shell wrapper does; no file means no flag. The Telegram note (env
tag, SESSION_BRIEF, app brief) rides every spawn as an `--append-system-prompt-file`
in the bot home, and each prompt reaches the CLI stdin exactly as typed."""

from __future__ import annotations

import asyncio
import json
import stat
from pathlib import Path

from tgforge.base.config import BotConfig
from tgforge.base.kernel import Kernel
from tgforge.plugins.claude import Claude, ClaudeTopic
from tgforge.plugins.claude.config import system_prompt_file
from tgforge.plugins.claude.driver import SESSION_BRIEF
from tgforge.testing import MockBot

_FAKE_CLI = """#!/usr/bin/env python3
import sys, json
for line in sys.stdin:
    with open(STDIN_LOG, "a") as f:
        f.write(line)
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


def _spawn_run(tmp_path, monkeypatch, workspace, prompts=("hi",), brief=None, resume=False):
    """Submit `prompts` one turn at a time through a fake CLI; return the spawn
    command, the prompt text of each stdin line the CLI received, and the note file's
    text as the CLI saw it (read before the window closes)."""
    import tgforge.plugins.claude.driver as drv

    stdin_log = tmp_path / "stdin.log"
    fake = tmp_path / "fakeclaude"
    fake.write_text(_FAKE_CLI.replace("STDIN_LOG", repr(str(stdin_log))))
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    captured = {}
    real = drv.asyncio.create_subprocess_exec

    async def spy(*cmd, **kw):
        captured["cmd"] = list(cmd)
        return await real(*cmd, **kw)

    monkeypatch.setattr(drv.asyncio, "create_subprocess_exec", spy)

    def lines():
        return stdin_log.read_text().splitlines() if stdin_log.exists() else []

    async def scenario():
        cfg = BotConfig(token="x", home=str(tmp_path), owner_id=1, chat_id=100, bot_username="bot")
        core = Kernel(MockBot(), cfg, [Claude(brief=brief)])
        claude = core.plugin_by_id["claude"]
        claude.claude_bin = str(fake)
        claude.claude_dir = tmp_path / ".claude"
        topic = core._instantiate(ClaudeTopic, 555, "work")
        topic.workspace = str(workspace)
        if resume:
            jsonl = topic._jsonl()
            jsonl.parent.mkdir(parents=True, exist_ok=True)
            jsonl.write_text(json.dumps({"type": "summary"}) + "\n")  # an existing session
        for n, prompt in enumerate(prompts, 1):
            await topic.submit(prompt)
            for _ in range(60):
                if len(lines()) >= n:
                    break
                await asyncio.sleep(0.1)
        cmd = captured["cmd"]
        if "--append-system-prompt-file" in cmd:
            note_path = Path(cmd[cmd.index("--append-system-prompt-file") + 1])
            captured["note"] = note_path.read_text()
        proc = topic.proc
        await topic.on_close()
        if proc is not None:
            await proc.wait()
        captured["note_left"] = "note" in captured and note_path.exists()

    asyncio.run(scenario())
    texts = [json.loads(line)["message"]["content"][0]["text"] for line in lines()]
    return captured["cmd"], texts, captured


def _spawned_cmd(tmp_path, monkeypatch, workspace) -> list[str]:
    return _spawn_run(tmp_path, monkeypatch, workspace)[0]


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


def _note(run) -> str:
    cmd, _texts, captured = run
    assert cmd.count("--append-system-prompt-file") == 1
    assert "--append-system-prompt" not in cmd  # no note text on the command line
    return captured["note"]


def test_spawn_appends_the_telegram_note_with_the_param_brief(tmp_path, monkeypatch):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    run = _spawn_run(tmp_path, monkeypatch, workspace, brief="house rules here")
    note = _note(run)
    assert note.startswith("[via Telegram bot · ")
    assert f"home {tmp_path}]" in note
    assert note.endswith(f"] {SESSION_BRIEF}\nhouse rules here")
    path = run[0][run[0].index("--append-system-prompt-file") + 1]
    assert path.startswith(f"{tmp_path}/")  # in the bot home
    assert not run[2]["note_left"]  # closing the window removes it


def test_spawn_appends_the_bot_home_session_brief(tmp_path, monkeypatch):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (tmp_path / "session_brief.md").write_text("from the home file\n")
    note = _note(_spawn_run(tmp_path, monkeypatch, workspace))
    assert note.endswith(f"] {SESSION_BRIEF}\nfrom the home file")


def test_spawn_note_without_an_app_brief_ends_at_session_brief(tmp_path, monkeypatch):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    assert _note(_spawn_run(tmp_path, monkeypatch, workspace)).endswith(f"] {SESSION_BRIEF}")


def test_a_brief_past_the_argument_cap_still_spawns(tmp_path, monkeypatch):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    big = "x" * 200_000  # over Linux's 128 KiB cap on one argument
    run = _spawn_run(tmp_path, monkeypatch, workspace, prompts=("hello",), brief=big)
    assert run[1] == ["hello"]  # the CLI started and got the prompt
    assert _note(run).endswith(big)


def test_spawn_note_sits_alongside_the_system_prompt_file(tmp_path, monkeypatch):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "system_prompt.txt").write_text("rules")
    run = _spawn_run(tmp_path, monkeypatch, workspace, brief="extra")
    cmd = run[0]
    flag = cmd.index("--system-prompt-file")
    assert cmd[flag + 1] == str(workspace / "system_prompt.txt")
    assert _note(run).endswith(f"{SESSION_BRIEF}\nextra")


def test_a_resumed_session_spawn_carries_the_note(tmp_path, monkeypatch):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    run = _spawn_run(tmp_path, monkeypatch, workspace, brief="house rules", resume=True)
    assert "--resume" in run[0] and "--session-id" not in run[0]
    assert _note(run).endswith(f"{SESSION_BRIEF}\nhouse rules")


def test_plain_and_slash_prompts_reach_the_cli_exactly_as_typed(tmp_path, monkeypatch):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    _, texts, _ = _spawn_run(
        tmp_path, monkeypatch, workspace, prompts=("hello there", "/sync"), brief="extra"
    )
    assert texts == ["hello there", "/sync"]
