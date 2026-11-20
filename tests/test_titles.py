"""Per-plugin title suffixes (`<ClassLabel> · <suffix>`), the rename guard
(unchanged title → no API call), and localfs bookmark storage. A rename is a
frame-skip title, so each check flushes the frame task first."""

from __future__ import annotations

import asyncio

from tgforge.plugins.claude import Claude, ClaudeTopic
from tgforge.plugins.localfs import Localfs, LocalfsTopic
from tgforge.plugins.shell import Shell, ShellTopic
from tgforge.testing import TestClient, kernel_bot


def _spy_renames(core):
    """Record the names passed to edit_forum_topic; returns the list."""
    calls: list[str] = []
    orig = kernel_bot(core).edit_forum_topic

    async def spy(**kw):
        calls.append(kw.get("name"))
        return await orig(**kw)

    kernel_bot(core).edit_forum_topic = spy
    return calls


def _instantiate(client, cls, cls_id, tid="555"):
    t = client.core._instantiate(cls, tid, cls.menu_label)
    client.core.owners[tid] = cls_id
    return t


# ── Composition ────────────────────────────────────────────────────────
def test_compose_title_joins_base_and_suffix(tmp_path):
    c = TestClient(Localfs(), home=str(tmp_path))
    assert c.core._compose_title("Files", "project") == "Files · project"
    assert c.core._compose_title("Files #2", "project") == "Files #2 · project"  # dedup kept
    assert c.core._compose_title("Files", None) == "Files"  # bare base, no suffix


def test_localfs_title_is_the_window_name(tmp_path):
    async def scenario():
        c = TestClient(Localfs(), home=str(tmp_path))
        t = _instantiate(c, LocalfsTopic, "localfs")
        t.saved["cwd"] = "/srv/data/myproj"
        await t.refresh_title()
        await c.core.flush_frames()
        assert c.core._names()["555"] == "📁 Files"  # no folder suffix

    asyncio.run(scenario())


def test_shell_suffix_is_cwd_basename(tmp_path):
    c = TestClient(Shell(), home=str(tmp_path))
    t = _instantiate(c, ShellTopic, "shell")
    t.saved["cwd"] = "/var/log/nginx"
    assert t.title_suffix() == "nginx"


def test_claude_session_title_becomes_the_topic_name(tmp_path):
    """The session title replaces the whole topic name (telegram_bot.py behavior),
    not a `Claude · <title>` suffix."""

    async def scenario():
        c = TestClient(Claude(), home=str(tmp_path))
        t = _instantiate(c, ClaudeTopic, "claude")
        t.title = "Fix the login bug"
        t.saved["base_name"] = t.title
        await t.refresh_title()
        await c.core.flush_frames()
        assert c.core._names()["555"] == "🤖 Fix the login bug"

    asyncio.run(scenario())


# ── Flood-safe rename guard ────────────────────────────────────────────
def test_unchanged_title_skips_the_api_call(tmp_path):
    async def scenario():
        c = TestClient(Shell(), home=str(tmp_path))
        calls = _spy_renames(c.core)
        t = _instantiate(c, ShellTopic, "shell")
        t.saved["cwd"] = "/srv/data/myproj"
        await t.refresh_title()
        await c.core.flush_frames()
        await t.refresh_title()  # same suffix → no second edit_forum_topic
        await c.core.flush_frames()
        assert calls == [f"{ShellTopic.icon} {ShellTopic.menu_label} · myproj"]

    asyncio.run(scenario())


# ── localfs bookmarks ──────────────────────────────────────────────────
def test_bookmark_add_remove(tmp_path):
    c = TestClient(Localfs(bookmarks=["/seed"]), home=str(tmp_path))
    fs = c.core.plugin_by_id["localfs"]
    assert fs.bookmarks == ["/seed"]  # seeded default
    assert fs.add_bookmark("/srv/work") == 2
    assert fs.add_bookmark("/srv/work") == 2  # dedup, no double-add
    assert "/srv/work" in fs.bookmarks
    removed = fs.remove_bookmark(0)
    assert removed == "/seed" and fs.bookmarks == ["/srv/work"]
    assert fs.remove_bookmark(9) is None  # out of range is a no-op


# ── Only /rename renames an agent window ──────────────────────────────
def test_a_session_title_in_the_claude_session_file_never_renames_the_topic(tmp_path):
    import json as _json

    from claude_fakes import FakeProc, assistant_text, drain, result, window

    async def scenario():
        c = TestClient(Claude(), home=str(tmp_path))
        renames = _spy_renames(c.core)
        t = window(c, tmp_path)
        (tmp_path / "none.jsonl").write_text(
            _json.dumps({"type": "ai-title", "aiTitle": "Retry bug hunt"}) + "\n"
        )

        async def spawn(*cmd, **kwargs):
            return FakeProc([assistant_text("ok"), result("ok")])

        t.spawn = spawn
        await t.submit("hi")
        await drain(c, t)
        await c.core.flush_frames()
        assert "ok" in [text for _m, _t, text in c.bot.created]  # the turn settled
        assert renames == [] and "base_name" not in t.saved and t.title is None

    asyncio.run(scenario())


def test_agys_title_mid_turn_renames_nothing_and_a_bare_rename_takes_it(tmp_path):
    from types import SimpleNamespace

    from agy_fakes import ACP, client, message, settle, window

    async def scenario():
        c = client(tmp_path)
        renames = _spy_renames(c.core)
        t = window(c, tmp_path)
        await t.submit("hi", reply_to=None)
        agent = t.agents[-1]
        agent.send(message("session_info_update", title="Named by agy"))
        await settle(c, t)
        assert t.turn_open  # it came mid-turn
        agent.send({**ACP["prompt_reply_end_turn"], "id": agent.prompt_ids()[-1]})
        await settle(c, t)
        await c.core.flush_frames()
        assert renames == [] and "base_name" not in t.saved and t.title is None
        assert t.agent_title == "Named by agy"
        await t.rename_cmd(SimpleNamespace(args=""))
        await c.core.flush_frames()
        assert t.saved["base_name"] == "Named by agy"
        assert renames == [f"{t.icon} Named by agy"]
        await t.on_close()

    asyncio.run(scenario())
