"""`kill_file_holders` stops a background job's whole process tree — found from the
processes holding its output file — and never reaches the caller's own group or an
unrelated reader's session."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

import tgforge.base.kernel as kernel

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="/proc probe")


def _tagged_alive(tag: str) -> list[int]:
    """Live (non-zombie) processes whose command line carries `tag`."""
    found = []
    for cmdline in Path("/proc").glob("[0-9]*/cmdline"):
        try:
            if tag in cmdline.read_bytes().decode(errors="replace"):
                pid = int(cmdline.parent.name)
                if kernel._alive(pid):
                    found.append(pid)
        except OSError:
            continue
    return found


def _wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError("condition never held")


def _job(out: Path, script: str) -> subprocess.Popen:
    """A job shell writing `out`, in its own session — as the CLI starts every
    background command."""
    with open(out, "w") as f:
        return subprocess.Popen(["sh", "-c", script], stdout=f, stderr=f, start_new_session=True)


def _cleanup(tag: str, *procs: subprocess.Popen) -> None:
    for pid in _tagged_alive(tag):
        os.kill(pid, 9)
    for proc in procs:
        if proc.poll() is None:
            proc.kill()
        proc.wait()


def test_kills_the_whole_job_tree(tmp_path):
    tag = f".{os.getpid()}7"  # unique sleep durations mark this run's processes
    out = tmp_path / f"job-{os.getpid()}.output"
    job = _job(
        out,
        f"sleep 301{tag} & "  # plain child
        f"timeout 302 sleep 303{tag} & "  # timeout moves itself into its own group
        f"setsid sleep 304{tag} & "  # its own session
        f"sleep 305{tag} >/dev/null 2>&1 & "  # does not hold the output file
        "wait",
    )
    try:
        _wait_for(lambda: len(_tagged_alive(tag)) == 6)  # sh, 4 sleeps, the timeout
        signalled = asyncio.run(kernel.kill_file_holders(str(out), grace=2.0))
        job.wait(timeout=5)
        _wait_for(lambda: _tagged_alive(tag) == [])
        assert signalled >= 6  # the shell + the 5 tagged processes
    finally:
        _cleanup(tag, job)


def test_expands_the_job_shells_session(tmp_path):
    """An orphan left in the job's session — not a holder, no longer a descendant —
    goes with the session its leader (the job shell) holds."""
    tag = f".{os.getpid()}6"
    out = tmp_path / f"orphan-{os.getpid()}.output"
    job = _job(out, f"(sleep 306{tag} >/dev/null 2>&1 </dev/null &); exec sleep 316{tag}")
    try:
        _wait_for(lambda: len(_tagged_alive(tag)) == 2)
        orphan = next(p for p in _tagged_alive(tag) if p != job.pid)
        assert kernel._process_table()[orphan][0] != job.pid  # reparented away
        asyncio.run(kernel.kill_file_holders(str(out), grace=2.0))
        job.wait(timeout=5)
        _wait_for(lambda: _tagged_alive(tag) == [])
    finally:
        _cleanup(tag, job)


def test_takes_a_descendant_that_left_the_session(tmp_path):
    """A child in its own session with its output redirected holds nothing and its
    session leader is no holder — only the descendant walk reaches it."""
    tag = f".{os.getpid()}5"
    out = tmp_path / f"desc-{os.getpid()}.output"
    job = _job(out, f"setsid sleep 307{tag} >/dev/null 2>&1 </dev/null & wait")
    try:
        _wait_for(lambda: len(_tagged_alive(tag)) == 2)  # sh + the setsid sleep
        asyncio.run(kernel.kill_file_holders(str(out), grace=2.0))
        job.wait(timeout=5)
        _wait_for(lambda: _tagged_alive(tag) == [])
    finally:
        _cleanup(tag, job)


def test_sigkills_what_ignores_sigterm(tmp_path):
    tag = f".{os.getpid()}4"
    out = tmp_path / f"stubborn-{os.getpid()}.output"
    job = _job(out, f"trap '' TERM; exec sleep 308{tag}")  # an ignored signal survives exec
    try:
        _wait_for(lambda: len(_tagged_alive(tag)) == 1)
        asyncio.run(kernel.kill_file_holders(str(out), grace=0.5))
        job.wait(timeout=5)
        assert job.returncode == -9  # it took the SIGKILL, not the SIGTERM
    finally:
        _cleanup(tag, job)


def test_leaves_an_unrelated_readers_session_alone(tmp_path):
    """A `tail -f` on the job's log from another terminal holds the file too; its
    session (the terminal's shell and whatever else it runs) must survive."""
    tag = f".{os.getpid()}3"
    out = tmp_path / f"shared-{os.getpid()}.output"
    job = _job(out, f"exec sleep 309{tag}")
    with open(os.devnull, "w") as devnull:
        terminal = subprocess.Popen(
            ["sh", "-c", f"tail -f {out} & sleep 310{tag}; wait"],
            stdout=devnull,
            stderr=devnull,
            start_new_session=True,
        )
    try:
        _wait_for(lambda: len(kernel.file_holders(str(out))) == 2)  # the job + tail
        asyncio.run(kernel.kill_file_holders(str(out), grace=2.0))
        job.wait(timeout=5)
        bystander = [p for p in _tagged_alive(tag) if p not in (job.pid, terminal.pid)]
        assert len(bystander) == 1  # the terminal's `sleep 310` still runs
        assert terminal.poll() is None  # and so does its shell
    finally:
        _cleanup(tag, job, terminal)


def _no_signals(monkeypatch) -> list:
    """Record, never send, every signal — a broken refusal must not hit this session."""
    sent = []
    monkeypatch.setattr(kernel.os, "kill", lambda pid, sig: sent.append((pid, sig)))
    return sent


def test_refuses_a_holder_in_the_callers_own_session(tmp_path, monkeypatch):
    out = tmp_path / f"own-{os.getpid()}.output"
    with open(out, "w") as f:
        child = subprocess.Popen(["sleep", "30"], stdout=f)  # same session as this process
    try:
        sent = _no_signals(monkeypatch)
        with pytest.raises(RuntimeError, match="own group"):
            asyncio.run(kernel.kill_file_holders(str(out)))
        assert sent == []  # refused before any signal
    finally:
        monkeypatch.undo()
        child.kill()
        child.wait()


def test_refuses_when_this_process_holds_the_file(tmp_path, monkeypatch):
    out = tmp_path / f"self-{os.getpid()}.output"
    out.write_text("x")
    sent = _no_signals(monkeypatch)
    with open(out):
        with pytest.raises(RuntimeError, match="own group"):
            asyncio.run(kernel.kill_file_holders(str(out)))
    assert sent == []


def test_nothing_holds_the_file(tmp_path):
    out = tmp_path / f"idle-{os.getpid()}.output"
    out.write_text("[exited with code 0]\n")
    assert asyncio.run(kernel.kill_file_holders(str(out))) == 0


def test_no_probe_off_linux(monkeypatch):
    monkeypatch.setattr(kernel, "IS_LINUX", False)
    assert kernel.file_holders("/x") is None
    assert asyncio.run(kernel.kill_file_holders("/x")) is None
