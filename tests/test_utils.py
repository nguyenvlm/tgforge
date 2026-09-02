"""Cross-platform process probes: the Linux path runs for real here; the macOS
path is driven by stubbing `lsof`."""

from __future__ import annotations

import os
import subprocess

import tgforge.base.kernel as utils


def test_pid_cwd_linux_reads_own_cwd(monkeypatch):
    monkeypatch.setattr(utils, "IS_LINUX", True)
    monkeypatch.setattr(utils, "IS_MACOS", False)
    assert utils.pid_cwd(os.getpid()) == os.getcwd()


def test_file_held_open_linux_true_for_open_file(monkeypatch, tmp_path):
    monkeypatch.setattr(utils, "IS_LINUX", True)
    monkeypatch.setattr(utils, "IS_MACOS", False)
    f = tmp_path / "held"
    f.write_text("x")
    with f.open():
        assert utils.file_held_open(str(f)) is True
    assert utils.file_held_open(str(f)) is False


def test_pid_cwd_macos_parses_lsof(monkeypatch):
    monkeypatch.setattr(utils, "IS_LINUX", False)
    monkeypatch.setattr(utils, "IS_MACOS", True)

    def fake_run(cmd, **kw):
        assert cmd[0] == "lsof"
        return subprocess.CompletedProcess(cmd, 0, stdout="p123\nfcwd\nn/some/dir\n")

    monkeypatch.setattr(utils.subprocess, "run", fake_run)
    assert utils.pid_cwd(123) == "/some/dir"


def test_file_held_open_macos_uses_lsof_returncode(monkeypatch):
    monkeypatch.setattr(utils, "IS_LINUX", False)
    monkeypatch.setattr(utils, "IS_MACOS", True)
    rc = {"code": 0}

    def fake_run(cmd, **kw):
        assert cmd[0] == "lsof"
        return subprocess.CompletedProcess(cmd, rc["code"])

    monkeypatch.setattr(utils.subprocess, "run", fake_run)
    assert utils.file_held_open("/x") is True
    rc["code"] = 1
    assert utils.file_held_open("/x") is False


def test_unknown_platform_defaults(monkeypatch):
    monkeypatch.setattr(utils, "IS_LINUX", False)
    monkeypatch.setattr(utils, "IS_MACOS", False)
    assert utils.pid_cwd(os.getpid()) is None  # no probe → unknown
    assert utils.file_held_open("/x") is True  # conservative: assume alive
