"""`kill_file_holders` stops a background job's whole process tree — found from the
processes holding its output file — and never reaches the caller's own group."""

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


def test_kills_the_whole_job_tree(tmp_path):
    tag = f".{os.getpid()}7"  # unique sleep durations mark this run's processes
    out = tmp_path / f"job-{os.getpid()}.output"
    script = (
        f"sleep 301{tag} & "  # plain child
        f"timeout 302 sleep 303{tag} & "  # timeout moves itself into its own group
        f"setsid sleep 304{tag} & "  # its own session
        f"sleep 305{tag} >/dev/null 2>&1 & "  # does not hold the output file
        "wait"
    )
    with open(out, "w") as f:
        # own session, as the CLI starts every background command
        job = subprocess.Popen(["sh", "-c", script], stdout=f, stderr=f, start_new_session=True)
    try:
        _wait_for(lambda: len(_tagged_alive(tag)) == 6)  # sh, 4 sleeps, the timeout
        signalled = asyncio.run(kernel.kill_file_holders(str(out), grace=2.0))
        job.wait(timeout=5)
        _wait_for(lambda: _tagged_alive(tag) == [])
        assert signalled >= 6  # the shell + the 5 tagged processes
    finally:
        for pid in _tagged_alive(tag):
            os.kill(pid, 9)
        if job.poll() is None:
            job.kill()


def test_refuses_a_holder_in_the_callers_own_session(tmp_path):
    out = tmp_path / f"own-{os.getpid()}.output"
    with open(out, "w") as f:
        child = subprocess.Popen(["sleep", "30"], stdout=f)  # same session as this process
    try:
        with pytest.raises(RuntimeError, match="own group"):
            asyncio.run(kernel.kill_file_holders(str(out)))
        assert child.poll() is None  # nothing was signalled
    finally:
        child.kill()
        child.wait()


def test_refuses_when_this_process_holds_the_file(tmp_path):
    out = tmp_path / f"self-{os.getpid()}.output"
    out.write_text("x")
    with open(out):
        with pytest.raises(RuntimeError, match="own group"):
            asyncio.run(kernel.kill_file_holders(str(out)))


def test_nothing_holds_the_file(tmp_path):
    out = tmp_path / f"idle-{os.getpid()}.output"
    out.write_text("[exited with code 0]\n")
    assert asyncio.run(kernel.kill_file_holders(str(out))) == 0


def test_no_probe_off_linux(monkeypatch):
    monkeypatch.setattr(kernel, "IS_LINUX", False)
    assert kernel.file_holders("/x") is None
    assert asyncio.run(kernel.kill_file_holders("/x")) is None
