"""Nested-menu command flows and the per-bot session-brief locator: the workspace
switch (a new cwd starts a fresh session), the list-editor helpers shared by the
typed path and the menu, and the `[via Telegram bot · …]` tag prepended to a turn."""

from __future__ import annotations

import asyncio

from tgforge.plugins.claude import Claude, ClaudeTopic
from tgforge.plugins.claude.driver import SESSION_BRIEF
from tgforge.testing import TestClient


def _topic(client, workspaces):
    claude = client.core.plugin_by_id["claude"]
    claude.workspaces = list(workspaces)
    t = client.core._instantiate(ClaudeTopic, 555, "work")
    t.workspace = str(workspaces[0]) if workspaces else ""
    t._save()
    return claude, t


class _FakeCtx:
    """Minimal stand-in for a typed command: only args + send are read."""

    def __init__(self, args):
        self.args = args
        self.sent: list[str] = []

    async def send(self, text):
        self.sent.append(text)


# ── Feature 2: the session-brief locator ───────────────────────────────
def test_env_tag_names_bot_manager_service_and_home(tmp_path):
    client = TestClient(Claude(), home=str(tmp_path))
    client.core.config.service = "mybot"
    _claude, t = _topic(client, [tmp_path])
    tag = t._env_tag()
    assert tag.startswith("[via Telegram bot · Mybot")
    assert "service `mybot`" in tag
    assert f"home {tmp_path}" in tag
    assert tag.endswith("]")


def test_env_tag_drops_service_when_unset(tmp_path):
    client = TestClient(Claude(), home=str(tmp_path), username="mybot")
    client.core.config.service = None
    _claude, t = _topic(client, [tmp_path])
    tag = t._env_tag()
    assert "Mybot" in tag  # falls back to the bot username, capitalized
    assert "service `" not in tag  # no service configured → the `service …` segment dropped


def test_brief_is_the_invariant_guidance_only():
    # the dynamic locator lives in _env_tag; the constant holds no bot identity and
    # no consumer-specific rules (those come from Claude(brief=) / the bot-home file)
    assert SESSION_BRIEF.startswith("Interactive tools")
    assert "via Telegram bot" not in SESSION_BRIEF
    assert "worktree" not in SESSION_BRIEF
    assert "main checkout" not in SESSION_BRIEF


def test_app_brief_from_param(tmp_path):
    client = TestClient(Claude(brief="house rules here"), home=str(tmp_path))
    _claude, t = _topic(client, [tmp_path])
    assert t._app_brief() == "house rules here"


def test_app_brief_from_bot_home_file(tmp_path):
    (tmp_path / "session_brief.md").write_text("from the home file\n")
    client = TestClient(Claude(), home=str(tmp_path))  # no brief= → fall back to the file
    _claude, t = _topic(client, [tmp_path])
    assert t._app_brief() == "from the home file"


def test_app_brief_absent_is_empty(tmp_path):
    client = TestClient(Claude(), home=str(tmp_path))  # no param, no file
    _claude, t = _topic(client, [tmp_path])
    assert t._app_brief() == ""


# ── Workspace-root + model helpers (shared by typed path and menu) ─────
def test_root_helpers_add_remove_and_text(tmp_path):
    client = TestClient(Claude(), home=str(tmp_path))
    claude = client.core.plugin_by_id["claude"]
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    n = claude.add_root(str(tmp_path / "*"))
    assert n == 2 and {p.name for p in claude.workspaces} == {"a", "b"}
    assert "resolved:" in claude.roots_text()
    removed = claude.remove_root(0)
    assert removed == str(tmp_path / "*") and claude.workspaces == []
    assert claude.remove_root(5) is None  # out of range is a no-op


def test_model_helpers_and_typed_path(tmp_path):
    async def scenario():
        client = TestClient(Claude(), home=str(tmp_path))
        claude = client.core.plugin_by_id["claude"]
        ctx = _FakeCtx("add Sonnet | claude-sonnet")
        await claude._models_typed(ctx)
        assert ["Sonnet", "claude-sonnet"] in claude.models
        ctx = _FakeCtx("add BareId")  # no `|` → label doubles as the id
        await claude._models_typed(ctx)
        assert ["BareId", "BareId"] in claude.models
        ctx = _FakeCtx("rm claude-sonnet")  # remove by value
        await claude._models_typed(ctx)
        assert all(v != "claude-sonnet" for _l, v in claude.models)
        claude.reset_models()
        assert claude.models == []

    asyncio.run(scenario())


# ── Feature 1: the workspace switch (fresh session on a new cwd) ───────
def test_switch_workspace_picks_and_starts_fresh_session(tmp_path):
    async def scenario():
        client = TestClient(Claude(), home=str(tmp_path))
        a = tmp_path / "a"
        a.mkdir()
        b = tmp_path / "b"
        b.mkdir()
        _claude, t = _topic(client, [a, b])
        old = t.session_id
        task = asyncio.create_task(t._switch_workspace())
        await client.tap("b")  # the menu labels a root by its dir name
        await asyncio.wait_for(task, 2)
        assert t.workspace == str(b)
        assert t.session_id != old  # a new cwd => a fresh session id
        return client

    client = asyncio.run(scenario())
    assert any("workspace · b" in r for r in client.replies)


def test_switch_workspace_no_change_keeps_session(tmp_path):
    async def scenario():
        client = TestClient(Claude(), home=str(tmp_path))
        a = tmp_path / "a"
        a.mkdir()
        b = tmp_path / "b"
        b.mkdir()
        _claude, t = _topic(client, [a, b])
        old = t.session_id
        task = asyncio.create_task(t._switch_workspace())
        await client.tap("a ✓")  # the already-active root is marked, re-picking is a no-op
        await asyncio.wait_for(task, 2)
        assert t.session_id == old

    asyncio.run(scenario())
