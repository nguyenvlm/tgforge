"""/cli hands back a ready-to-paste command to continue this same session in a PC
terminal — the right workspace, account config dir, and session id, with a clear
instruction so it reads as an action, not a bare line."""

from __future__ import annotations

import asyncio

from tgforge.plugins.claude import Claude, ClaudeTopic
from tgforge.testing import TestClient


def test_cli_emits_a_clear_resume_command(tmp_path):
    async def scenario():
        c = TestClient(Claude(), home=str(tmp_path))
        t = c.core._instantiate(ClaudeTopic, 555, "work")
        t.workspace = str(tmp_path)

        await t.cli(None)

        msg = "\n".join(c.replies)
        assert "paste this into a terminal" in msg  # says what to do, not a bare line
        assert "```" in msg  # the command sits in a tap-to-copy code block
        assert f"claude --resume {t.session_id}" in msg  # this exact session
        assert f"cd {t.workspace}" in msg  # in its workspace
        assert "CLAUDE_CONFIG_DIR=" in msg  # under its account

    asyncio.run(scenario())
