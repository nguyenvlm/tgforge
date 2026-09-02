"""The SQLite-backed store: namespaced dict semantics, per-write persistence,
namespace enumeration/drop for window rebuild, and the daily usage counters."""

from __future__ import annotations

import pytest

from tgforge.base.kernel import AppDB, core_ns, plugin_ns, window_ns


def test_dict_semantics_and_namespacing(tmp_path):
    db = AppDB(tmp_path / "state.db")
    a = db.saved(plugin_ns("claude"))
    b = db.saved(window_ns("shell", 7))

    a["model"] = "opus"
    a["accounts"] = {"work": "/x"}
    b["cwd"] = "/tmp"

    assert a["model"] == "opus"
    assert a.get("missing", "d") == "d"
    assert "model" in a and "cwd" not in a  # namespaces are isolated
    assert b["cwd"] == "/tmp"
    assert sorted(a.keys()) == ["accounts", "model"]
    assert a.as_dict() == {"model": "opus", "accounts": {"work": "/x"}}

    del a["model"]
    assert "model" not in a
    assert a.pop("accounts") == {"work": "/x"}
    assert a.pop("accounts", None) is None
    with pytest.raises(KeyError):
        _ = a["nope"]


def test_persists_across_reopen(tmp_path):
    db = AppDB(tmp_path / "state.db")
    db.saved(core_ns())["name_registry"] = {"100": "shell"}
    db.close()
    again = AppDB(tmp_path / "state.db")
    assert again.saved(core_ns())["name_registry"] == {"100": "shell"}


def test_namespaces_and_drop_for_window_rebuild(tmp_path):
    db = AppDB(tmp_path / "state.db")
    db.saved(window_ns("shell", 1))["cwd"] = "/a"
    db.saved(window_ns("claude", 2))["session_id"] = "x"
    db.saved(plugin_ns("claude"))["model"] = "opus"

    assert db.namespaces("win:") == ["win:claude:2", "win:shell:1"]
    db.drop(window_ns("shell", 1))
    assert db.namespaces("win:") == ["win:claude:2"]
    assert "cwd" not in db.saved(window_ns("shell", 1))


def test_usage_counts(tmp_path):
    db = AppDB(tmp_path / "state.db")
    db.bump("2026-08-24", "claude", "/claude")
    db.bump("2026-08-24", "claude", "/claude")
    db.bump("2026-08-25", "claude", "/claude")
    db.bump("2026-08-24", "shell", "/c")
    totals = db.usage_totals()
    assert totals[0] == ("claude", "/claude", 3)  # most-used first
    assert ("shell", "/c", 1) in totals
