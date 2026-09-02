"""`tgforge workspaces` resolves a real bot home — the sole installed app, a
named app, or an explicit --home — and never the ~/.tgforge parent dir (which
would scatter stray state)."""

from __future__ import annotations

from pathlib import Path

import tgforge.cli as cli


def test_explicit_home_wins(tmp_path):
    assert cli._resolve_home(None, str(tmp_path)) == tmp_path.resolve()


def test_named_app_maps_under_tgforge(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert cli._resolve_home("bravo", None) == tmp_path / ".tgforge" / "bravo"


def test_single_installed_app_is_picked(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr("tgforge.install._list_apps", lambda: ["solo"])
    assert cli._resolve_home(None, None) == (tmp_path / ".tgforge" / "solo").resolve()


def test_ambiguous_or_none_returns_none(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr("tgforge.install._list_apps", lambda: ["a", "b"])
    assert cli._resolve_home(None, None) is None  # needs --app; never the parent
    monkeypatch.setattr("tgforge.install._list_apps", lambda: [])
    assert cli._resolve_home(None, None) is None


def test_never_resolves_the_parent_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr("tgforge.install._list_apps", lambda: ["solo"])
    resolved = cli._resolve_home(None, None)
    assert resolved != (tmp_path / ".tgforge").resolve()
    assert resolved is not None and resolved.parent == (tmp_path / ".tgforge").resolve()


def test_path_import_available():
    assert isinstance(cli._resolve_home("x", None), Path)
