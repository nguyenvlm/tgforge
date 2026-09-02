"""`tgforge uninstall <name>` removes exactly that bot's service + home, and
--keep-home leaves the state behind. The systemd call is stubbed."""

from __future__ import annotations

import tgforge.base.service as service
from tgforge.install import _list_apps, _select, uninstall


def test_select_falls_back_to_numbered_input(monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)  # no TTY → numbered prompt
    monkeypatch.setattr("builtins.input", lambda *a: "2")
    assert _select("pick", ["a", "b", "c"]) == "b"
    monkeypatch.setattr("builtins.input", lambda *a: "c")
    assert _select("pick", ["a", "b", "c"]) == "c"  # by name too
    monkeypatch.setattr("builtins.input", lambda *a: "zzz")
    assert _select("pick", ["a", "b"]) is None  # no match


def test_list_apps_finds_bot_json_dirs(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".tgforge" / "alpha").mkdir(parents=True)
    (tmp_path / ".tgforge" / "alpha" / "bot.json").write_text("{}")
    (tmp_path / ".tgforge" / "beta").mkdir(parents=True)  # no bot.json → skipped
    assert _list_apps() == ["alpha"]


def test_uninstall_removes_named_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    called: list[str] = []
    monkeypatch.setattr(service, "uninstall_unit", lambda name: called.append(name))
    home = tmp_path / ".tgforge" / "demo"
    home.mkdir(parents=True)
    (home / "bot.json").write_text("{}")
    uninstall("demo", assume_yes=True)
    assert called == ["demo"]  # service named after the app
    assert not home.exists()


def test_uninstall_keep_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(service, "uninstall_unit", lambda name: None)
    home = tmp_path / ".tgforge" / "demo"
    home.mkdir(parents=True)
    uninstall("demo", keep_home=True)
    assert home.exists()
