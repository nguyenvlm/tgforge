"""The agy window's accounts: account data dirs and their scan, the PTY login, `/cli`
and `agy-as` (the `HOME=` form), a non-default account's data dir, and the account
switch carrying the session."""

from __future__ import annotations

import asyncio
import stat

from agy_fakes import (
    SESSION,
    client,
    session_db,
    window,
)

from tgforge.plugins.agy import Agy, driver, setup
from tgforge.plugins.agy.config import TOKEN_FILE, data_dir
from tgforge.testing import TestClient


def _signed_in(home):
    token = data_dir(home) / TOKEN_FILE
    token.parent.mkdir(parents=True, exist_ok=True)
    token.write_text("")
    return home


def test_accounts_are_homes_holding_a_token(tmp_path):
    plugin = TestClient(Agy(), home=str(tmp_path)).core.plugin_by_id["agy"]
    plugin.accounts_root = tmp_path
    _signed_in(tmp_path / ".agy-work")
    (tmp_path / ".agy-empty" / ".gemini").mkdir(parents=True)
    _signed_in(tmp_path)  # the default account: the real home's .gemini
    assert plugin.scan_accounts() == {"work": str(tmp_path / ".agy-work")}
    assert plugin.account_dir_for("x") == tmp_path / ".agy-x"
    assert plugin.account_name(tmp_path / ".agy-work") == "work"
    assert plugin.account_name(tmp_path) == "default"
    plugin.saved["accounts"] = {"empty": str(tmp_path / ".agy-empty")}
    rows = {r["name"]: r["label"] for r in plugin.account_status()}
    assert rows == {
        "default": "✅ default — signed in",
        "empty": "❌ empty — not signed in",
        "work": "✅ work — signed in",
    }


_LOGIN = """#!/usr/bin/env python3
import os, sys, pathlib
args = sys.argv[1:]
assert args[0] == "acp" and args[-1] == "--login", args
assert sys.stdin.isatty() and "DISPLAY" not in os.environ
gemini = pathlib.Path(args[args.index("--gemini-dir") + 1])
print("Go to https://accounts.example/auth?x=1 and paste the code", flush=True)
code = sys.stdin.readline().strip()
if code == "good":
    (gemini / "antigravity-cli").mkdir(parents=True, exist_ok=True)
    (gemini / "antigravity-cli" / "antigravity-oauth-token").write_text("")
    sys.exit(0)
sys.exit(1)
"""


class _FlowIO:
    def __init__(self, *answers):
        self.sent, self.asked = [], []
        self._answers = list(answers)

    async def send(self, text, **kw):
        self.sent.append(text)

    async def ask_text(self, prompt, timeout=None):
        self.asked.append((prompt, timeout))
        return self._answers.pop(0) if self._answers else None

    async def spawn(self, *args, **kwargs):
        return await asyncio.create_subprocess_exec(*args, **kwargs)


def _login_plugin(tmp_path, monkeypatch):
    fake = tmp_path / "agy"
    fake.write_text(_LOGIN)
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("DISPLAY", ":0")
    plugin = TestClient(Agy(), home=str(tmp_path)).core.plugin_by_id["agy"]
    plugin.accounts_root = tmp_path
    plugin.agy_bin = str(fake)
    return plugin


def test_login_runs_agy_acp_login_in_a_pty_and_types_the_code(tmp_path, monkeypatch):
    plugin = _login_plugin(tmp_path, monkeypatch)
    home = tmp_path / ".agy-work"

    async def scenario():
        io = _FlowIO("good")
        assert await plugin._run_login(io, "work", home) is True
        assert "https://accounts.example/auth?x=1" in io.asked[0][0]
        assert io.asked[0][1] == driver.LOGIN_WINDOW
        assert (data_dir(home) / TOKEN_FILE).exists()
        io = _FlowIO("bad")
        assert await plugin._run_login(io, "other", tmp_path / ".agy-other") is False
        assert any("didn't complete" in t for t in io.sent)

    asyncio.run(scenario())


def test_cli_and_agy_as_use_home_for_a_non_default_account(tmp_path, monkeypatch):
    async def scenario():
        c = client(tmp_path)
        t = window(c, tmp_path)
        t.session_id = SESSION
        assert t._cli_line() == f"cd {tmp_path} && agy --conversation {SESSION}"
        t.config_dir = str(tmp_path / ".agy-work")
        assert t._cli_line() == (
            f"cd {tmp_path} && HOME={tmp_path / '.agy-work'} agy --conversation {SESSION}"
        )

    asyncio.run(scenario())
    monkeypatch.setenv("HOME", str(tmp_path))
    setup.install_agy_as()
    script = (tmp_path / ".local" / "bin" / "agy-as").read_text()
    assert 'exec env HOME="$HOME/.agy-$name" agy "$@"' in script


def test_a_non_default_account_runs_with_its_gemini_dir(tmp_path):
    async def scenario():
        c = client(tmp_path)
        t = window(c, tmp_path)
        t.config_dir = str(tmp_path / ".agy-work")
        await t.submit("hi")
        assert t.agents[0].args == [
            t.plugin.agy_bin,
            "acp",
            "--gemini-dir",
            str(tmp_path / ".agy-work" / ".gemini"),
        ]
        await t.on_close()

    asyncio.run(scenario())


def test_an_account_switch_carries_the_session_database_and_brain(tmp_path):
    async def scenario():
        c = client(tmp_path)
        t = window(c, tmp_path)
        t.session_id = SESSION
        session_db(tmp_path)
        brain = data_dir(tmp_path) / "brain" / SESSION / "x.md"
        brain.parent.mkdir(parents=True)
        brain.write_text("notes")
        new_home = tmp_path / ".agy-work"
        t._switch_account(tmp_path, new_home, str(new_home))
        assert (data_dir(new_home) / "conversations" / f"{SESSION}.db").exists()
        assert (data_dir(new_home) / "brain" / SESSION / "x.md").read_text() == "notes"
        assert t.config_dir == str(new_home)

    asyncio.run(scenario())
