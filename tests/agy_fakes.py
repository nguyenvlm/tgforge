"""Shared fakes for agy-window tests: a scripted `agy acp` process answering from the
captured payloads (`fixtures/agy_acp.jsonl`, `fixtures/agy_transcript.jsonl`), and a
ready window. The thread helpers (`screen`, `texts`, `drain`) are claude_fakes'."""

from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path

from claude_fakes import THREAD, screen

from tgforge.plugins.agy import Agy, AgyTopic
from tgforge.plugins.agy import driver as agy_driver
from tgforge.plugins.agy.config import data_dir
from tgforge.testing import TestClient

FIXTURES = Path(__file__).resolve().parent / "fixtures"
ACP = {
    row["name"]: row["message"]
    for row in map(json.loads, (FIXTURES / "agy_acp.jsonl").read_text().splitlines())
}
STEPS = {
    row["name"]: row["step"]
    for row in map(json.loads, (FIXTURES / "agy_transcript.jsonl").read_text().splitlines())
}
SESSION = ACP["session_new_result"]["result"]["sessionId"]
USAGE = json.loads((FIXTURES / "agy_usage.json").read_text())
PS_TABLE = json.loads((FIXTURES / "agy_run_command_ps.json").read_text())
# the captured table as the kernel reads it: pid → (ppid, pgid, sid), pid → argv
AGY_PID, EXE_PID, BASH_PID, SLEEP_PID = (r["pid"] for r in PS_TABLE)
TABLE = {r["pid"]: (r["ppid"], r["pid"], r["pid"]) for r in PS_TABLE}
ARGV = {r["pid"]: r["argv"] for r in PS_TABLE}
JOB_COMMAND = ARGV[BASH_PID][2]  # the captured background run_command's CommandLine


def message(fixture: str, **update) -> dict:
    """A captured `session/update` (or other agent message) with `update` fields set."""
    msg = copy.deepcopy(ACP[fixture])
    if "params" in msg:
        msg["params"]["sessionId"] = SESSION
        target = msg["params"].get("update", msg["params"])
        target.update(update)
    return msg


def text_chunk(kind: str, text: str) -> dict:
    """An `agent_message_chunk` / `agent_thought_chunk` with `text`."""
    return message(kind, content={"type": "text", "text": text})


def step(fixture: str, **fields) -> dict:
    out = copy.deepcopy(STEPS[fixture])
    out.update(fields)
    return out


class _Stdin:
    def __init__(self, agent):
        self._agent = agent
        self._buf = b""

    def write(self, data: bytes):
        self._buf += data
        while b"\n" in self._buf:
            line, self._buf = self._buf.split(b"\n", 1)
            self._agent.received.append(json.loads(line))
            self._agent.answer(self._agent.received[-1])

    async def drain(self):
        if self._agent.returncode is not None:
            raise BrokenPipeError

    def close(self):
        pass

    def is_closing(self):
        return False


class _Stdout:
    def __init__(self):
        self.queue: asyncio.Queue = asyncio.Queue()

    def __aiter__(self):
        return self

    async def __anext__(self):
        line = await self.queue.get()
        if line is None:
            raise StopAsyncIteration
        return line


class FakeAgent:
    """A scripted `agy acp`: answers initialize, session/new, session/load (after the
    history replay `replay`), and set_config_option from the captures; records every
    message the window wrote; the test sends the rest (`send`)."""

    def __init__(self, args=(), replay=()):
        self.args = list(args)
        self.pid = 0
        self.returncode = None
        self.received: list[dict] = []
        self.stdin = _Stdin(self)
        self.stdout = _Stdout()
        self.replay = list(replay)
        self.answers_close = True

    def send(self, msg: dict) -> None:
        self.stdout.queue.put_nowait((json.dumps(msg) + "\n").encode())

    def reply(self, request_id, name: str) -> None:
        msg = copy.deepcopy(ACP[name])
        msg["id"] = int(request_id) if str(request_id).isdigit() else request_id
        self.send(msg)

    def answer(self, msg: dict) -> None:
        method = msg.get("method")
        if method == "initialize":
            self.reply(msg["id"], "initialize_result")
        elif method == "session/new":
            self.reply(msg["id"], "session_new_result")
        elif method == "session/load":
            for line in self.replay:
                self.send(line)
            self.reply(msg["id"], "session_load_result")
        elif method == "session/set_config_option":
            self.reply(msg["id"], "set_config_option_result")
        elif method == "session/close" and self.answers_close:
            self.send({"jsonrpc": "2.0", "id": msg["id"], "result": {}})

    def requests(self, method: str) -> list[dict]:
        return [m for m in self.received if m.get("method") == method]

    def prompt_ids(self) -> list[int]:
        return [m["id"] for m in self.requests("session/prompt")]

    def exit(self) -> None:
        """The process ends: its stdout closes."""
        self.returncode = 0
        self.stdout.queue.put_nowait(None)

    def kill(self):
        if self.returncode is None:
            self.returncode = -9
            self.stdout.queue.put_nowait(None)

    async def wait(self):
        return self.returncode


class FakeUsage:
    """A scripted `agy --output-format json -p=/usage` run: prints `payload` (the
    captured one by default) and exits with `returncode`."""

    def __init__(self, args, env, payload=None, returncode=0, hang=None):
        self.args, self.env = list(args), env
        self.payload = USAGE if payload is None else payload
        self.returncode = None
        self.pid = 0
        self._exit = returncode
        self._hang = hang  # an Event: the run answers only once it is set

    async def communicate(self):
        if self._hang is not None:
            await self._hang.wait()
        self.returncode = self._exit
        body = self.payload if isinstance(self.payload, bytes) else json.dumps(self.payload)
        return (body.encode() if isinstance(body, str) else body), b""

    def kill(self):
        self.returncode = -9

    async def wait(self):
        return self.returncode


def client(tmp_path, **plugin_args) -> TestClient:
    plugin = Agy(**plugin_args)
    plugin.accounts_root = tmp_path  # the account homes and the default `.gemini` live here
    return TestClient(plugin, home=str(tmp_path))


def window(c: TestClient, tmp_path, thread: str = THREAD, replay=()) -> AgyTopic:
    """An agy window without the on_open pickers; its spawn starts a `FakeAgent`
    (`t.agents`, newest last), or for a `/usage` poll a `FakeUsage` answering with
    `t.usage_payload` / `t.usage_returncode` (`t.usage_polls`)."""
    t = c.core._instantiate(AgyTopic, thread, "work")
    c.core.owners[thread] = "agy"
    t.workspace = str(tmp_path)
    t.agents, t.usage_polls = [], []
    t.usage_payload, t.usage_returncode, t.usage_hang = None, 0, None

    async def spawn(*args, **kwargs):
        if "-p=/usage" in args:
            poll = FakeUsage(
                args, kwargs.get("env"), t.usage_payload, t.usage_returncode, t.usage_hang
            )
            t.usage_polls.append(poll)
            return poll
        agent = FakeAgent(args, replay)
        t.agents.append(agent)
        return agent

    t.spawn = spawn
    return t


def transcript(t: AgyTopic) -> Path:
    path = t._transcript_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def write_steps(t: AgyTopic, *steps: dict) -> None:
    with open(transcript(t), "a") as f:
        for s in steps:
            f.write(json.dumps(s) + "\n")


def session_db(tmp_path: Path, account_home: Path | None = None) -> Path:
    """Mark the session as stored (its conversation database) in the account's data dir."""
    db = data_dir(account_home or tmp_path) / "conversations" / f"{SESSION}.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    db.write_bytes(b"")
    return db


def last_message(c: TestClient, thread: str = THREAD) -> str:
    """The bottom-most turn message: the newest one that is not the jobs panel."""
    return next(t for _mid, t in reversed(screen(c, thread)) if not t.startswith("background ·"))


async def settle(c: TestClient, t: AgyTopic, rounds: int = 5) -> None:
    """Let the agent's lines be read, the send steps run and the frames land."""
    for _ in range(rounds):
        await asyncio.sleep(0.02)
        await t._sent()
        await c.core.flush_frames()


def fast_transcript(monkeypatch) -> None:
    monkeypatch.setattr(agy_driver, "TRANSCRIPT_POLL", 0.01)
