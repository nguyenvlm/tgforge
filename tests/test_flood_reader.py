"""The panel-freeze regression: a Telegram flood-wait on panel edits must never stall
the reader. Drives a full event stream through `_reader` while every edit floods, and
asserts the reader drains every event and finalizes — bounded, not frozen for minutes.

Before the fix, `_call` slept `retry_after` (up to minutes) inline, so a flooding edit
froze the reader: stdout undrained, panel dead, then a dump. Now droppable panel edits
skip and essential ones cap at FLOOD_WAIT_CAP."""

from __future__ import annotations

import asyncio
import json

from aiogram.exceptions import TelegramRetryAfter

from tgforge.base.kernel import FLOOD_WAIT_CAP
from tgforge.plugins.claude import ClaudeTopic
from tgforge.testing import TestClient


class _Flood(TelegramRetryAfter):
    def __init__(self, retry_after=300):
        self.retry_after = retry_after


class _FakeStdin:
    def write(self, b):
        pass

    async def drain(self):
        pass

    def close(self):
        pass


class _FakeStdout:
    """An async byte-line iterator standing in for the CLI's stdout pipe."""

    def __init__(self, lines):
        self._lines = list(lines)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._lines:
            raise StopAsyncIteration
        return self._lines.pop(0)


class _FakeProc:
    def __init__(self, lines):
        self.returncode = None
        self.stdin = _FakeStdin()
        self.stdout = _FakeStdout(lines)

    def kill(self):
        self.returncode = -9


def _events():
    ev = [
        {
            "type": "assistant",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "hi"}]},
        },
        {"type": "stream_event", "event": {"type": "message_delta", "usage": {"output_tokens": 5}}},
        {"type": "result", "result": "done", "subtype": "success", "is_error": False},
    ]
    return [(json.dumps(e) + "\n").encode() for e in ev]


def test_reader_drains_and_finalizes_while_edits_flood(tmp_path, monkeypatch):
    async def scenario():
        slept = []
        real_sleep = asyncio.sleep
        monkeypatch.setattr(asyncio, "sleep", lambda s: slept.append(s) or real_sleep(0))

        c = TestClient(home=str(tmp_path))
        # every panel edit floods; sends succeed so the holder can open + the card can post
        monkeypatch.setattr(
            c.bot, "edit_message_text", lambda *a, **k: (_ for _ in ()).throw(_Flood())
        )

        t = c.core._instantiate(ClaudeTopic, 555, "work")
        t.workspace = str(tmp_path)
        t._sync_title = lambda: asyncio.sleep(0)
        t._save = lambda: None
        t._jsonl = lambda: tmp_path / "none.jsonl"
        t.proc = _FakeProc(_events())

        # the reader must return on its own (all events drained → result → proc killed)
        await asyncio.wait_for(t._reader(), timeout=5)

        assert t.busy is False  # finalized, not stuck busy
        assert t.proc is None  # reader tore the turn down cleanly
        # no unbounded wait: every sleep is capped, never the raw 300s retry_after
        assert all(s <= FLOOD_WAIT_CAP for s in slept), slept

    asyncio.run(scenario())


def test_reorder_keeps_old_holder_when_send_is_dropped(tmp_path, monkeypatch):
    async def scenario():
        c = TestClient(home=str(tmp_path))
        # the reorder's new-holder send floods (droppable → dropped, returns None)
        monkeypatch.setattr(c.bot, "send_message", lambda *a, **k: (_ for _ in ()).throw(_Flood()))
        deleted = []
        monkeypatch.setattr(c.bot, "delete_message", lambda cid, mid: deleted.append(mid))

        t = c.core._instantiate(ClaudeTopic, 555, "work")
        t.holder_id = 42
        t.turn_start = asyncio.get_event_loop().time()
        await t._reorder_holder()

        assert t.holder_id == 42  # old holder kept, not lost to a dropped send
        assert deleted == []  # nothing deleted when there is no replacement

    asyncio.run(scenario())
