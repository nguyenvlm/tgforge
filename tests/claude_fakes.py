"""Shared fakes for Claude-window tests: a scripted CLI process, a ready window, and the
thread as the user sees it (messages in chat order, deletes and edits applied)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from tgforge.plugins.claude import Claude, ClaudeTopic
from tgforge.testing import TestClient

THREAD = 555
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "cli_usage_events.jsonl"


class FakeStdin:
    def __init__(self):
        self.writes: list[bytes] = []

    def write(self, b):
        self.writes.append(b)

    async def drain(self):
        pass

    def close(self):
        pass

    def is_closing(self):
        return False


class FakeStdout:
    """Yields scripted lines; an `asyncio.Event` in the list holds the stream there
    until it is set (a CLI that has not printed the next event yet)."""

    def __init__(self, lines):
        self._lines = list(lines)
        self.consumed = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        while self._lines and isinstance(self._lines[0], asyncio.Event):
            await self._lines.pop(0).wait()
        if not self._lines:
            raise StopAsyncIteration
        self.consumed += 1
        return self._lines.pop(0)


class FakeProc:
    def __init__(self, lines=()):
        self.returncode = None
        self.stdin = FakeStdin()
        self.stdout = FakeStdout(lines)

    def kill(self):
        self.returncode = -9


def line(ev: dict) -> bytes:
    return (json.dumps(ev) + "\n").encode()


def result(text: str, **extra) -> bytes:
    return line({"type": "result", "result": text, "subtype": "success", **extra})


def assistant_text(text: str) -> bytes:
    return line({"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}})


def tool_use(name: str, tool_input: dict) -> bytes:
    block = {"type": "tool_use", "id": "t1", "name": name, "input": tool_input}
    return line({"type": "assistant", "message": {"content": [block]}})


def replay(*texts: str) -> bytes:
    content = [{"type": "text", "text": t} for t in texts]
    return line({"type": "user", "message": {"role": "user", "content": content}})


def fixture_events() -> dict[str, dict]:
    """The captured CLI events by kind: `message_start`, `rate_limit_event`, `result`."""
    out = {}
    for raw in FIXTURE.read_text().splitlines():
        ev = json.loads(raw)
        kind = ev["event"]["type"] if ev["type"] == "stream_event" else ev["type"]
        out[kind] = ev
    return out


def client(tmp_path) -> TestClient:
    return TestClient(Claude(), home=str(tmp_path))


def window(c: TestClient, tmp_path, thread: int = THREAD) -> ClaudeTopic:
    """A Claude window without the interactive on_open pickers, on an empty session."""
    t = c.core._instantiate(ClaudeTopic, thread, "work")
    c.core.owners[thread] = "claude"
    t.workspace = str(tmp_path)
    t._jsonl = lambda: tmp_path / "none.jsonl"
    return t


async def drain(c: TestClient, t: ClaudeTopic) -> None:
    """Let every queued send step run and every frame land."""
    for _ in range(3):
        await t._sent()
        await c.core.flush_frames()


def screen(c: TestClient, thread: int = THREAD) -> list[tuple[int, str]]:
    """The thread as it reads now: (message id, current text) in chat order."""
    edits = dict(c.bot.edits)  # the last edit per message wins
    return [
        (mid, edits.get(mid, text))
        for mid, tid, text in c.bot.created
        if tid == thread and mid not in c.bot.deleted
    ]


def texts(c: TestClient, thread: int = THREAD) -> list[str]:
    return [text for _mid, text in screen(c, thread)]


async def cancel_task(task) -> None:
    """Stop a background task (the updater, a painter) and let it unwind."""
    if task is not None:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
