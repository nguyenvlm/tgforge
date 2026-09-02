"""Per-plugin title suffixes (`<ClassLabel> · <suffix>`), the flood-safe rename
guard (unchanged title → no API call), and localfs bookmark storage."""

from __future__ import annotations

import asyncio

from tgforge.plugins.claude import Claude, ClaudeTopic
from tgforge.plugins.localfs import Localfs, LocalfsTopic
from tgforge.plugins.shell import Shell, ShellTopic
from tgforge.testing import TestClient


def _spy_renames(core):
    """Record the names passed to edit_forum_topic; returns the list."""
    calls: list[str] = []
    orig = core.bot.edit_forum_topic

    async def spy(**kw):
        calls.append(kw.get("name"))
        return await orig(**kw)

    core.bot.edit_forum_topic = spy
    return calls


def _instantiate(client, cls, cls_id, tid=555):
    t = client.core._instantiate(cls, tid, cls.menu_label)
    client.core.owners[tid] = cls_id
    return t


# ── Composition ────────────────────────────────────────────────────────
def test_compose_title_joins_base_and_suffix(tmp_path):
    c = TestClient(Localfs(), home=str(tmp_path))
    assert c.core._compose_title("Files", "project") == "Files · project"
    assert c.core._compose_title("Files #2", "project") == "Files #2 · project"  # dedup kept
    assert c.core._compose_title("Files", None) == "Files"  # bare base, no suffix


def test_localfs_suffix_is_folder_name(tmp_path):
    async def scenario():
        c = TestClient(Localfs(), home=str(tmp_path))
        t = _instantiate(c, LocalfsTopic, "localfs")
        t.saved["cwd"] = "/srv/data/myproj"
        await t.refresh_title()
        assert c.core._names()[555] == "📁 Files · myproj"

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
        assert c.core._names()[555] == "🤖 Fix the login bug"

    asyncio.run(scenario())


def _write_jsonl(t, tmp_path, lines):
    import json as _json
    from pathlib import Path

    from tgforge.plugins.claude.driver import _project_dir

    t.workspace = str(tmp_path / "ws")
    t.config_dir = str(tmp_path / "cfg")
    jsonl = _project_dir(Path(t.config_dir), Path(t.workspace)) / f"{t.session_id}.jsonl"
    jsonl.parent.mkdir(parents=True, exist_ok=True)
    jsonl.write_text("".join(_json.dumps(ev) + "\n" for ev in lines))


def test_sync_title_renames_topic_to_the_ai_title(tmp_path):
    async def scenario():
        c = TestClient(Claude(), home=str(tmp_path))
        t = _instantiate(c, ClaudeTopic, "claude")
        _write_jsonl(t, tmp_path, [{"type": "ai-title", "aiTitle": "Retry bug hunt"}])
        await t._sync_title()
        assert c.core._names()[555] == "🤖 Retry bug hunt"

    asyncio.run(scenario())


def test_sync_title_custom_outranks_ai(tmp_path):
    """Same priority as telegram_bot.py: a custom-title (user /rename) beats the
    auto ai-title regardless of order."""

    async def scenario():
        c = TestClient(Claude(), home=str(tmp_path))
        t = _instantiate(c, ClaudeTopic, "claude")
        _write_jsonl(
            t,
            tmp_path,
            [
                {"type": "custom-title", "customTitle": "My name"},
                {"type": "ai-title", "aiTitle": "Auto name"},
            ],
        )
        await t._sync_title()
        assert c.core._names()[555] == "🤖 My name"

    asyncio.run(scenario())


# ── Flood-safe rename guard ────────────────────────────────────────────
def test_unchanged_title_skips_the_api_call(tmp_path):
    async def scenario():
        c = TestClient(Localfs(), home=str(tmp_path))
        calls = _spy_renames(c.core)
        t = _instantiate(c, LocalfsTopic, "localfs")
        t.saved["cwd"] = "/srv/data/myproj"
        await t.refresh_title()
        await t.refresh_title()  # same suffix → no second edit_forum_topic
        assert calls == ["📁 Files · myproj"]

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
