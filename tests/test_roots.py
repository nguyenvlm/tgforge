"""Workspace roots live in the one state.db under the agent plugins' shared store —
offline edits (install / cli) and the running bot read the same key, and the
db sits flat in home (no nested .tgforge)."""

from __future__ import annotations

from pathlib import Path

from tgforge.base.kernel import AppDB, plugin_ns, shared_ns
from tgforge.plugins.agent.window import ROOTS_STORE, read_roots, write_roots
from tgforge.plugins.agy import Agy
from tgforge.plugins.claude import Claude
from tgforge.testing import TestClient


def test_roots_roundtrip_through_state_db(tmp_path: Path):
    assert read_roots(tmp_path) == []  # nothing yet
    write_roots(tmp_path, ["/a", "/b"])
    assert read_roots(tmp_path) == ["/a", "/b"]


def test_roots_land_where_the_running_bot_reads_them(tmp_path: Path):
    write_roots(tmp_path, ["/ws"])
    # the plugin's _load does exactly this at runtime
    store = AppDB(tmp_path / "state.db").saved(shared_ns(ROOTS_STORE))
    assert store.get("roots") == ["/ws"]


def test_state_db_is_flat_in_home(tmp_path: Path):
    write_roots(tmp_path, ["/ws"])
    assert (tmp_path / "state.db").exists()  # sibling of bot.json
    assert not (tmp_path / ".tgforge").exists()  # no nested dir


def test_roots_under_the_old_claude_key_move_to_the_shared_store_once(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    AppDB(tmp_path / "state.db").saved(plugin_ns("claude"))["roots"] = [str(root)]
    c = TestClient(Agy(), home=str(tmp_path))
    plugin = c.core.plugin_by_id["agy"]
    plugin._load()
    assert plugin.workspaces == [root.resolve()]
    db = AppDB(tmp_path / "state.db")
    assert db.saved(shared_ns(ROOTS_STORE)).get("roots") == [str(root)]
    assert db.saved(plugin_ns("claude")).get("roots") is None  # moved, not copied
    db.saved(plugin_ns("claude"))["roots"] = ["/stale"]  # a later old-key write never wins
    plugin._load()
    assert plugin.roots == [str(root)]


def test_an_agy_only_app_sees_roots_added_through_the_cli(tmp_path):
    from tgforge.cli import _workspaces

    root = tmp_path / "ws"
    root.mkdir()
    _workspaces("add", str(root), None, str(tmp_path))
    assert read_roots(tmp_path) == [str(root)]
    c = TestClient(Agy(), home=str(tmp_path))
    plugin = c.core.plugin_by_id["agy"]
    plugin._load()
    assert plugin.workspaces == [root.resolve()]


def test_both_agent_plugins_share_one_roots_list(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    c = TestClient(Claude(), Agy(), home=str(tmp_path))
    claude, agy = c.core.plugin_by_id["claude"], c.core.plugin_by_id["agy"]
    claude._load()
    agy._load()
    claude.add_root(str(root))
    assert agy.workspaces == [root.resolve()]
    db = AppDB(tmp_path / "state.db")
    assert db.saved(shared_ns(ROOTS_STORE)).get("roots") == [str(root)]  # written shared
    assert db.saved(plugin_ns("claude")).get("roots") is None
    assert read_roots(tmp_path) == [str(root)]
