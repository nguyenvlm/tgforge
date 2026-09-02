"""Workspace roots live in the one state.db under the claude plugin namespace —
offline edits (install / cli) and the running bot read the same key, and the
db sits flat in home (no nested .tgforge)."""

from __future__ import annotations

from pathlib import Path

from tgforge.base.kernel import AppDB, plugin_ns
from tgforge.plugins.claude.config import read_roots, write_roots


def test_roots_roundtrip_through_state_db(tmp_path: Path):
    assert read_roots(tmp_path) == []  # nothing yet
    write_roots(tmp_path, ["/a", "/b"])
    assert read_roots(tmp_path) == ["/a", "/b"]


def test_roots_land_where_the_running_bot_reads_them(tmp_path: Path):
    write_roots(tmp_path, ["/ws"])
    # the plugin's _load does exactly this at runtime
    store = AppDB(tmp_path / "state.db").saved(plugin_ns("claude"))
    assert store.get("roots") == ["/ws"]


def test_state_db_is_flat_in_home(tmp_path: Path):
    write_roots(tmp_path, ["/ws"])
    assert (tmp_path / "state.db").exists()  # sibling of bot.json
    assert not (tmp_path / ".tgforge").exists()  # no nested dir
