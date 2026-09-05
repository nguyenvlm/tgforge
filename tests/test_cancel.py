"""`/cancel` acknowledges in every state and never pipes a redundant interrupt.

Before this, cancel() was a silent no-op the moment `busy` flipped False — even with
the CLI process still alive holding a running background job — so pressing it again
did nothing (the user saw '/cancel stopped responding' with a job stuck in the panel).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from tgforge.plugins.claude import ClaudeTopic
from tgforge.testing import TestClient


class _FakeStdin:
    def __init__(self):
        self.writes: list[bytes] = []

    def write(self, b):
        self.writes.append(b)

    async def drain(self):
        pass


class _FakeProc:
    def __init__(self):
        self.returncode = None
        self.stdin = _FakeStdin()

    def kill(self):
        self.returncode = -9


_CTX = SimpleNamespace(args="")


def test_cancel_idle_with_running_background_job_still_responds(tmp_path):
    async def scenario():
        c = TestClient(home=str(tmp_path))
        t = c.core._instantiate(ClaudeTopic, 555, "work")
        t.busy = False
        t.proc = _FakeProc()  # process kept alive for the background job
        t.background_tasks = {"bid1": {"label": "job", "start": 0.0, "done": None}}
        await t.cancel(_CTX)
        assert any("background job(s) still going" in r for r in c.replies)

    asyncio.run(scenario())


def test_cancel_idle_nothing_running(tmp_path):
    async def scenario():
        c = TestClient(home=str(tmp_path))
        t = c.core._instantiate(ClaudeTopic, 555, "work")
        t.busy = False
        t.proc = None
        await t.cancel(_CTX)
        assert c.replies and c.replies[-1] == "nothing to cancel — no turn is running"

    asyncio.run(scenario())


def test_cancel_dedupes_while_busy(tmp_path):
    async def scenario():
        c = TestClient(home=str(tmp_path))
        t = c.core._instantiate(ClaudeTopic, 555, "work")
        t.busy = True
        t.proc = _FakeProc()

        await t.cancel(_CTX)
        assert t.cancel_requested is True
        assert c.replies[-1] == "cancelling..."
        writes_after_first = len(t.proc.stdin.writes)
        assert writes_after_first == 1  # exactly one interrupt piped

        await t.cancel(_CTX)  # a second press while the same turn is still cancelling
        assert c.replies[-1] == "already cancelling..."
        assert len(t.proc.stdin.writes) == writes_after_first  # no redundant interrupt

    asyncio.run(scenario())
