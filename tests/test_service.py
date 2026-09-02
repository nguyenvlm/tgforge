"""Service helpers: the macOS backend writes a launchd plist + issues launchctl
calls (systemd/launchctl themselves are stubbed)."""

from __future__ import annotations

import subprocess

import tgforge.base.service as service


def _capture(monkeypatch):
    calls: list[list[str]] = []
    monkeypatch.setattr(
        service.subprocess,
        "run",
        lambda cmd, **kw: calls.append(cmd) or subprocess.CompletedProcess(cmd, 0),
    )
    return calls


def test_macos_install_writes_plist_and_loads(tmp_path, monkeypatch):
    monkeypatch.setattr(service, "IS_MACOS", True)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(service, "_exec_start", lambda: "/opt/tgforge")
    calls = _capture(monkeypatch)

    dest = service.install_unit("demo", tmp_path / "home")
    assert dest == tmp_path / "Library" / "LaunchAgents" / "demo.plist"
    body = dest.read_text()
    assert "<key>Label</key><string>demo</string>" in body
    assert "/opt/tgforge" in body and "ExitTimeOut" in body
    assert ["launchctl", "load", "-w", str(dest)] in calls


def test_macos_uninstall_unloads_and_removes(tmp_path, monkeypatch):
    monkeypatch.setattr(service, "IS_MACOS", True)
    monkeypatch.setenv("HOME", str(tmp_path))
    dest = tmp_path / "Library" / "LaunchAgents" / "demo.plist"
    dest.parent.mkdir(parents=True)
    dest.write_text("x")
    calls = _capture(monkeypatch)

    service.uninstall_unit("demo")
    assert not dest.exists()
    assert ["launchctl", "unload", "-w", str(dest)] in calls


def test_macos_service_active_reads_launchctl_list(monkeypatch):
    monkeypatch.setattr(service, "IS_MACOS", True)
    monkeypatch.setattr(
        service.subprocess,
        "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout='{ "PID" = 42; }'),
    )
    assert service.service_active("demo") is True
    monkeypatch.setattr(
        service.subprocess,
        "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout="{ };"),
    )
    assert service.service_active("demo") is False


def test_macos_detached_restart_spawns_waiter(tmp_path, monkeypatch):
    monkeypatch.setattr(service, "IS_MACOS", True)
    spawned: list[list[str]] = []
    monkeypatch.setattr(service.subprocess, "Popen", lambda cmd, **kw: spawned.append(cmd) or None)
    service.detached_restart("demo", tmp_path)
    assert len(spawned) == 1
    script = spawned[0][2]
    assert "kickstart -k" in script and str(tmp_path) in script
