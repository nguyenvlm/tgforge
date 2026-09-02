"""Test-wide guards.

`_no_leaked_children` fails any test that spawns a subprocess and leaves it alive at
teardown — the deterministic catch for an orphaned OS process (a window whose child was
never killed, or a TUI grandchild in its own process group). It complements the
`error::ResourceWarning` filter, which catches the asyncio-transport side of a leak.
Stdlib /proc scan; a no-op off Linux (the check simply finds no children).
"""

from __future__ import annotations

import os
import time

import pytest


def _own_children() -> set[int]:
    """PIDs whose parent is this test process (stdlib /proc; empty off Linux)."""
    me = os.getpid()
    kids: set[int] = set()
    try:
        entries = os.listdir("/proc")
    except OSError:
        return kids
    for name in entries:
        if not name.isdigit():
            continue
        try:
            with open(f"/proc/{name}/stat", "rb") as f:
                after_comm = f.read().rsplit(b")", 1)[1].split()
            ppid = int(after_comm[1])  # fields after 'comm': state, ppid, …
        except (OSError, ValueError, IndexError):
            continue
        if ppid == me:
            kids.add(int(name))
    return kids


@pytest.fixture(autouse=True)
def _no_leaked_children():
    before = _own_children()
    yield
    # Grace: a child that exited on its own is reaped by asyncio's watcher thread a beat
    # later; only a child still parented here past the deadline is a real leak.
    deadline = time.monotonic() + 0.5
    leaked = _own_children() - before
    while leaked and time.monotonic() < deadline:
        time.sleep(0.02)
        leaked = _own_children() - before
    assert not leaked, f"test leaked child process(es): {sorted(leaked)}"
