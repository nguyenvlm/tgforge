"""`/login` reports every account's credential state, then offers a re-login menu only
when something is expired (the healthy case just reports and returns). The re-login flow
(`_run_login` / `login_new_account`) is driven against a fake `claude auth login` that
prints a URL, reads the pasted code, and writes credentials on success."""

from __future__ import annotations

import asyncio
import json
import stat
import time

from tgforge.plugins.claude import Claude
from tgforge.testing import TestClient

# A fake `claude auth login --claudeai`: FAKE_LOGIN_MODE picks the outcome —
# ok (URL → read code → write creds → exit 0), nourl (no URL, exit 1),
# nocreds (URL → read code → exit 1 without writing creds).
_LOGIN_CLI = """#!/usr/bin/env python3
import sys, os, json, pathlib
mode = os.environ.get("FAKE_LOGIN_MODE", "ok")
if mode == "nourl":
    print("no browser available", flush=True)
    sys.exit(1)
print("Open https://claude.ai/oauth?code to sign in", flush=True)
code = sys.stdin.readline().strip()
if mode == "ok" and code:
    d = pathlib.Path(os.environ["CLAUDE_CONFIG_DIR"])
    d.mkdir(parents=True, exist_ok=True)
    (d / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {"subscriptionType": "pro"}}))
    sys.exit(0)
sys.exit(1)
"""


def _fake_login_cli(tmp_path):
    p = tmp_path / "fakelogin"
    p.write_text(_LOGIN_CLI)
    p.chmod(p.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return str(p)


class _FlowIO:
    """A stand-in topic/ctx for the login flow: scripted ask_text answers, recorded sends."""

    def __init__(self, *answers):
        self.sent = []
        self._answers = list(answers)

    async def send(self, text, **kw):
        self.sent.append(text)

    async def ask_text(self, prompt, timeout=None):
        return self._answers.pop(0) if self._answers else None


def _write_ok(dir_path):
    now = int(time.time() * 1000)
    dir_path.mkdir(parents=True, exist_ok=True)
    (dir_path / ".credentials.json").write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "subscriptionType": "pro",
                    "expiresAt": now + 5 * 86_400_000,
                    "refreshTokenExpiresAt": now + 30 * 86_400_000,
                }
            }
        )
    )


class _FakeCtx:
    def __init__(self, choice=None):
        self.sent = []
        self.menu_calls = []
        self._choice = choice

    async def send(self, text, **kw):
        self.sent.append(text)

    async def menu(self, title, options):
        self.menu_calls.append((title, options))
        return self._choice


def _plugin(tmp_path):
    p = Claude()
    p.claude_dir = tmp_path / ".claude"
    p.accounts = {}
    return p


def test_login_all_healthy_reports_no_menu(tmp_path):
    _write_ok(tmp_path / ".claude")  # the default account is healthy

    async def scenario():
        ctx = _FakeCtx()
        await _plugin(tmp_path).login_cmd(ctx)
        assert any("login status" in t for t in ctx.sent)
        assert not ctx.menu_calls  # nothing expired → no re-login prompt

    asyncio.run(scenario())


def test_login_offers_relogin_when_expired(tmp_path):
    now = int(time.time() * 1000)
    (tmp_path / ".claude-dead").mkdir(parents=True)
    (tmp_path / ".claude-dead" / ".credentials.json").write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "subscriptionType": "pro",
                    "expiresAt": now - 1,
                    "refreshTokenExpiresAt": now - 1,
                }
            }
        )
    )

    async def scenario():
        ctx = _FakeCtx(choice=None)  # Cancel the re-login menu
        await _plugin(tmp_path).login_cmd(ctx)
        assert ctx.menu_calls  # a re-login menu was offered
        _title, options = ctx.menu_calls[0]
        labels = [label for label, _v in options]
        assert "dead" in labels and "➕ Log in new account" in labels

    asyncio.run(scenario())


# ── the login subprocess flow ──────────────────────────────────────


def test_run_login_success_writes_creds(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_LOGIN_MODE", "ok")
    plugin = _plugin(tmp_path)
    plugin.claude_bin = _fake_login_cli(tmp_path)
    cfg = tmp_path / ".claude-work"

    async def scenario():
        io = _FlowIO("the-pasted-code")
        assert await plugin._run_login(io, "work", cfg) is True
        assert (cfg / ".credentials.json").exists()

    asyncio.run(scenario())


def test_run_login_no_url_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_LOGIN_MODE", "nourl")
    plugin = _plugin(tmp_path)
    plugin.claude_bin = _fake_login_cli(tmp_path)

    async def scenario():
        io = _FlowIO("unused")
        assert await plugin._run_login(io, "work", tmp_path / ".claude-work") is False
        assert any("no URL" in t for t in io.sent)

    asyncio.run(scenario())


def test_run_login_cancelled_code_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_LOGIN_MODE", "ok")
    plugin = _plugin(tmp_path)
    plugin.claude_bin = _fake_login_cli(tmp_path)

    async def scenario():
        io = _FlowIO()  # ask_text returns None → the user never pastes a code
        assert await plugin._run_login(io, "work", tmp_path / ".claude-work") is False
        assert any("timed out" in t for t in io.sent)

    asyncio.run(scenario())


def test_run_login_no_creds_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_LOGIN_MODE", "nocreds")
    plugin = _plugin(tmp_path)
    plugin.claude_bin = _fake_login_cli(tmp_path)

    async def scenario():
        io = _FlowIO("the-code")
        assert await plugin._run_login(io, "work", tmp_path / ".claude-work") is False
        assert any("didn't complete" in t for t in io.sent)

    asyncio.run(scenario())


def test_login_new_account_success_records_and_saves(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_LOGIN_MODE", "ok")
    c = TestClient(Claude(), home=str(tmp_path))
    plugin = c.core.plugin_by_id["claude"]
    plugin.claude_bin = _fake_login_cli(tmp_path)
    plugin.claude_dir = tmp_path / ".claude"

    async def scenario():
        topic = _FlowIO("work", "the-code")  # name, then code
        result = await plugin.login_new_account(topic)
        assert result is not None
        name, cfg = result
        assert name == "work"
        assert plugin.accounts["work"] == cfg  # recorded
        assert plugin.saved["accounts"]["work"] == cfg  # persisted

    asyncio.run(scenario())


def test_login_new_account_name_cancelled(tmp_path):
    async def scenario():
        topic = _FlowIO()  # no name → cancelled
        assert await _plugin(tmp_path).login_new_account(topic) is None

    asyncio.run(scenario())


def test_login_new_account_invalid_name(tmp_path):
    async def scenario():
        topic = _FlowIO("///")  # sanitises to empty
        assert await _plugin(tmp_path).login_new_account(topic) is None
        assert any("invalid name" in t for t in topic.sent)

    asyncio.run(scenario())
