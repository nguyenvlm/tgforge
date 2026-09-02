"""`tgforge restart` loads the bot home's config and schedules a detached restart,
passing the requester thread from TELEGRAM_THREAD_ID; a home with no `service` is
refused instead of restarting."""

from __future__ import annotations

import json

import tgforge.base.service as service
from tgforge import cli


def _home(tmp_path, service_name):
    home = tmp_path / "home"
    home.mkdir()
    cfg = {"token": "x", "home": str(home)}
    if service_name is not None:
        cfg["service"] = service_name
    (home / "bot.json").write_text(json.dumps(cfg))
    return home


def test_restart_schedules_with_thread_from_env(tmp_path, monkeypatch):
    home = _home(tmp_path, "mybot")
    calls = []
    monkeypatch.setattr(
        service,
        "detached_restart",
        lambda svc, h, announce_thread=None: calls.append((svc, str(h), announce_thread)),
    )
    monkeypatch.setenv("TELEGRAM_THREAD_ID", "42")
    cli.main(["restart", "--home", str(home)])
    assert calls == [("mybot", str(home), 42)]


def test_restart_without_thread_env_passes_none(tmp_path, monkeypatch):
    home = _home(tmp_path, "mybot")
    calls = []
    monkeypatch.setattr(
        service,
        "detached_restart",
        lambda svc, h, announce_thread=None: calls.append(announce_thread),
    )
    monkeypatch.delenv("TELEGRAM_THREAD_ID", raising=False)
    cli.main(["restart", "--home", str(home)])
    assert calls == [None]


def test_restart_without_service_is_refused(tmp_path, monkeypatch, capsys):
    home = _home(tmp_path, None)
    calls = []
    monkeypatch.setattr(service, "detached_restart", lambda *a, **k: calls.append(a))
    cli.main(["restart", "--home", str(home)])
    assert not calls  # never scheduled
    assert "no service configured" in capsys.readouterr().out
