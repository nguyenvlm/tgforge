"""Claude plugin smoke tests: registration, and one full turn driven against a
fake CLI that speaks stream-json (a user replay + a result). Live Telegram + real
CLI parity still needs a running bot."""

from __future__ import annotations

import asyncio
import stat
from types import SimpleNamespace

from tgforge.base.config import BotConfig
from tgforge.base.kernel import Kernel
from tgforge.plugins.claude import Claude, ClaudeTopic

FAKE_CLI = """#!/usr/bin/env python3
import sys, json
line = sys.stdin.readline()
try:
    prompt = json.loads(line)["message"]["content"][0]["text"]
except Exception:
    prompt = "?"
print(json.dumps({"type": "user", "message": {"role": "user",
      "content": [{"type": "text", "text": prompt}]}}), flush=True)
print(json.dumps({"type": "result", "result": "ANSWER " + prompt[:12],
      "subtype": "success", "is_error": False}), flush=True)
"""


class MockBot:
    def __init__(self):
        self._n = 100
        self.sent: list[str] = []
        self.edited: list[str] = []

    async def get_me(self):
        return SimpleNamespace(username="bot")

    async def send_message(self, chat_id, text, **kw):
        self._n += 1
        self.sent.append(text)
        return SimpleNamespace(message_id=self._n)

    async def edit_message_text(self, text, **kw):
        self.edited.append(text)
        return SimpleNamespace(message_id=kw.get("message_id"))

    async def edit_message_reply_markup(self, **kw):
        return SimpleNamespace(message_id=kw.get("message_id"))

    async def delete_message(self, **kw):
        return True

    async def create_forum_topic(self, chat_id, name):
        self._n += 1
        return SimpleNamespace(message_thread_id=self._n)

    async def delete_forum_topic(self, **kw):
        return True

    async def edit_forum_topic(self, **kw):
        return True


def _fake_cli(tmp_path):
    p = tmp_path / "fakeclaude"
    p.write_text(FAKE_CLI)
    p.chmod(p.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return str(p)


def _kernel(tmp_path):
    cfg = BotConfig(token="x", home=str(tmp_path), owner_id=1, chat_id=100, bot_username="bot")
    return Kernel(MockBot(), cfg, [Claude()])


def test_registration(tmp_path):
    core = _kernel(tmp_path)
    assert core.registry.launches["/claude"][0] == "claude"
    assert "!" in core.registry.prefixes and "!!" in core.registry.prefixes
    assert "agent.prompt" in core.registry.services
    assert "/cli" in core.registry.classes["claude"].commands


def test_one_turn_against_fake_cli(tmp_path):
    async def scenario():
        core = _kernel(tmp_path)
        claude = core.plugin_by_id["claude"]
        claude.claude_bin = _fake_cli(tmp_path)
        claude.claude_dir = tmp_path / ".claude"
        claude.workspaces = [tmp_path]
        # build a window without the interactive on_open pickers
        t = core._instantiate(ClaudeTopic, 555, "work")
        t.workspace = str(tmp_path)

        await t.submit("hello there")

        async def answered():
            return any("ANSWER" in e for e in core.bot.edited)

        waited = 0.0
        while waited < 6.0 and not await answered():
            await asyncio.sleep(0.1)
            waited += 0.1
        assert await answered()
        await t.on_close()

    asyncio.run(scenario())
