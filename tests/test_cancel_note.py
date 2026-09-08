"""The '/cancel' bubble must never stick at 'cancelling…'. It is settled to
'✖️ cancelled' at every turn-end boundary — the finalize path (a cancelled result) and
the reader teardown (a cancel that ended via a dead proc) — via _settle_cancel_note."""

from __future__ import annotations

import asyncio

from tgforge.plugins.claude import ClaudeTopic
from tgforge.testing import TestClient


class _FakeStdin:
    def write(self, b):
        pass

    async def drain(self):
        pass

    def close(self):
        pass


class _FakeStdout:
    def __init__(self, lines):
        self._lines = list(lines)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._lines:
            raise StopAsyncIteration
        return self._lines.pop(0)


class _FakeProc:
    def __init__(self, lines=()):
        self.returncode = None
        self.stdin = _FakeStdin()
        self.stdout = _FakeStdout(lines)

    def kill(self):
        self.returncode = -9


def _ready(c, tmp_path):
    t = c.core._instantiate(ClaudeTopic, 555, "work")
    t.workspace = str(tmp_path)
    t._sync_title = lambda: asyncio.sleep(0)
    t._save = lambda: None
    t._jsonl = lambda: tmp_path / "none.jsonl"
    t.background_tasks = {}
    t.turn_start = asyncio.get_event_loop().time()
    return t


def test_cancel_bubble_is_settled_when_the_turn_finalizes(tmp_path, monkeypatch):
    async def scenario():
        real_sleep = asyncio.sleep
        monkeypatch.setattr(asyncio, "sleep", lambda s: real_sleep(0))
        c = TestClient(home=str(tmp_path))
        t = _ready(c, tmp_path)
        t.holder_id = 42
        t.busy = True
        t.proc = _FakeProc()

        await t.cancel(None)  # sends "cancelling…", remembers its id
        assert t.cancel_msg_id is not None
        note_id = t.cancel_msg_id

        await t._finalize_turn("done")

        assert t.cancel_msg_id is None  # settled, not left dangling
        assert any(mid == note_id and "cancelled" in text for mid, text in c.bot.edits)

    asyncio.run(scenario())


def test_cancel_bubble_is_settled_when_the_proc_dies(tmp_path, monkeypatch):
    """No result event — the turn ends via the reader teardown. The bubble is still
    settled (the shared boundary catches it), never left at 'cancelling…'."""

    async def scenario():
        real_sleep = asyncio.sleep
        monkeypatch.setattr(asyncio, "sleep", lambda s: real_sleep(0))
        c = TestClient(home=str(tmp_path))
        t = _ready(c, tmp_path)
        t.cancel_msg_id = await t.send("cancelling...")  # a cancel was requested earlier
        note_id = t.cancel_msg_id
        t.proc = _FakeProc([])  # empty stream → reader exits into its finally teardown

        await asyncio.wait_for(t._reader(), timeout=5)

        assert t.cancel_msg_id is None
        assert any(mid == note_id and "cancelled" in text for mid, text in c.bot.edits)

    asyncio.run(scenario())
