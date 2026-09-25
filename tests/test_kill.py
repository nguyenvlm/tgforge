"""`/kill <job>` stops a background job through the CLI that owns it (`stop_task`), and
falls back to killing the job's process tree when no live CLI can be asked."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from tgforge.plugins.claude import ClaudeTopic, background
from tgforge.plugins.claude import driver as driver_module
from tgforge.testing import TestClient


class _FakeStdin:
    """Records each control request with how many messages were sent before it."""

    def __init__(self, bot, on_drain=None):
        self.bot = bot
        self.writes: list[tuple[int, dict]] = []  # (len(bot.sent) at write time, request)
        self.on_drain = on_drain

    def write(self, b):
        self.writes.append((len(self.bot.sent), json.loads(b)))

    async def drain(self):
        if self.on_drain is not None:
            self.on_drain(self.writes[-1][1])


class _FakeProc:
    def __init__(self, bot, on_drain=None):
        self.returncode = None
        self.stdin = _FakeStdin(bot, on_drain)

    def kill(self):
        self.returncode = -9


def _topic(c, tmp_path, jobs):
    t = c.core._instantiate(ClaudeTopic, 555, "work")
    t.busy = False
    t.background_tasks = {
        bid: {"path": str(tmp_path / f"{bid}.output"), "label": label, "start": 0.0, "done": done}
        for bid, label, done in jobs
    }
    return t


def _ctx(args=""):
    return SimpleNamespace(args=args)


def _respond(t, request, subtype="success", error=None):
    response = {"subtype": subtype, "request_id": request["request_id"], "response": {}}
    if error:
        response["error"] = error
    t._resolve_control({"type": "control_response", "response": response})


def _track_ids(bot) -> dict[int, str]:
    """message id → text of every message the bot sends from now on."""
    ids: dict[int, str] = {}
    real = bot.send_message

    async def send_message(chat_id, text, **kw):
        msg = await real(chat_id, text, **kw)
        ids[msg.message_id] = text
        return msg

    bot.send_message = send_message
    return ids


async def _pump_until(predicate, n=200):
    for _ in range(n):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition never held")


def test_kill_sends_stop_task_then_the_note(tmp_path):
    async def scenario():
        c = TestClient(home=str(tmp_path))
        t = _topic(c, tmp_path, [("b1", "tests", None)])
        t.proc = _FakeProc(c.bot)
        task = asyncio.create_task(t.kill_job(_ctx("tests")))
        await _pump_until(lambda: t.proc.stdin.writes and "stopping tests…" in c.replies)
        sent_before_write, request = t.proc.stdin.writes[0]
        assert request["request"] == {"subtype": "stop_task", "task_id": "b1"}
        assert len(t.proc.stdin.writes) == 1
        # the stdin write comes first, then the note is the next message
        assert c.replies[sent_before_write:] == ["stopping tests…"]
        assert "✖ stopping" in background.panel(t)[1]
        _respond(t, request)
        await task
        assert c.replies[sent_before_write:] == ["stopping tests…"]  # success adds nothing
        assert t.control_waiters == {}

    asyncio.run(scenario())


def test_kill_reports_the_cli_error_and_keeps_the_row_running(tmp_path):
    async def scenario():
        c = TestClient(home=str(tmp_path))
        t = _topic(c, tmp_path, [("b1", "tests", None)])
        t.proc = _FakeProc(c.bot)
        task = asyncio.create_task(t.kill_job(_ctx("tests")))
        await _pump_until(lambda: t.proc.stdin.writes)
        _respond(t, t.proc.stdin.writes[0][1], subtype="error", error="task not found")
        await task
        assert c.replies[-1] == "couldn't stop tests: task not found"
        assert t.background_tasks["b1"]["done"] is None
        assert not t.background_tasks["b1"]["stopping"]

    asyncio.run(scenario())


def test_kill_gets_a_response_resolved_during_the_write(tmp_path):
    """The reader can answer while drain() yields — the waiter must already exist."""

    async def scenario():
        c = TestClient(home=str(tmp_path))
        t = _topic(c, tmp_path, [("b1", "tests", None)])
        t.proc = _FakeProc(c.bot, on_drain=lambda request: _respond(t, request))
        await asyncio.wait_for(t.kill_job(_ctx("tests")), 1)
        assert c.replies == ["stopping tests…"]

    asyncio.run(scenario())


def test_kill_fails_its_wait_when_the_cli_exits(tmp_path):
    async def scenario():
        c = TestClient(home=str(tmp_path))
        t = _topic(c, tmp_path, [("b1", "tests", None)])
        t.proc = _FakeProc(c.bot)
        task = asyncio.create_task(t.kill_job(_ctx("tests")))
        await _pump_until(lambda: t.proc.stdin.writes)
        t._fail_control_waiters()  # the reader's teardown on a dead proc
        await asyncio.wait_for(task, 1)
        assert c.replies[-1] == "couldn't stop tests: the Claude process ended"

    asyncio.run(scenario())


def test_kill_unknown_or_ambiguous_name_writes_nothing(tmp_path):
    async def scenario():
        c = TestClient(home=str(tmp_path))
        jobs = [("b1", "tests unit", None), ("b2", "tests e2e", None), ("b3", "build", None)]
        t = _topic(c, tmp_path, jobs)
        t.proc = _FakeProc(c.bot)
        await t.kill_job(_ctx("deploy"))
        assert c.replies[-1] == (
            "no running job matches 'deploy' — running: tests unit, tests e2e, build"
        )
        await t.kill_job(_ctx("tests"))
        assert c.replies[-1] == "'tests' matches several jobs: tests unit, tests e2e — name one"
        assert t.proc.stdin.writes == []

    asyncio.run(scenario())


def test_kill_with_no_running_jobs(tmp_path):
    async def scenario():
        c = TestClient(home=str(tmp_path))
        t = _topic(c, tmp_path, [("b1", "old", "✓")])
        t.proc = _FakeProc(c.bot)
        await t.kill_job(_ctx())
        assert c.replies[-1] == "no running background jobs"
        assert t.proc.stdin.writes == []

    asyncio.run(scenario())


def test_kill_without_a_name_offers_running_jobs_only(tmp_path):
    async def scenario():
        c = TestClient(home=str(tmp_path))
        t = _topic(c, tmp_path, [("b1", "tests", None), ("b2", "old", "✓"), ("b3", "build", None)])
        t.proc = _FakeProc(c.bot)
        task = asyncio.create_task(t.kill_job(_ctx()))
        await _pump_until(lambda: c.buttons)
        labels = [b.split(" · ")[0] for b in c.buttons if " · " in b]
        assert labels == ["tests", "build"]
        await c.tap(next(b for b in c.buttons if b.startswith("build")))
        await _pump_until(lambda: t.proc.stdin.writes)
        assert t.proc.stdin.writes[0][1]["request"]["task_id"] == "b3"
        _respond(t, t.proc.stdin.writes[0][1])
        await asyncio.wait_for(task, 1)

    asyncio.run(scenario())


def test_kill_mid_turn_keeps_the_card_below_the_note(tmp_path, monkeypatch):
    """Mid-turn, the note goes above the live card, so the finalized reply (the card,
    edited in place) stays the bottom-most message; the turn's bookkeeping is untouched."""

    async def scenario():
        c = TestClient(home=str(tmp_path))
        ids = _track_ids(c.bot)
        t = _topic(c, tmp_path, [("b1", "tests", None)])
        t.proc = _FakeProc(c.bot)
        t.busy = True
        t.turn_start = asyncio.get_event_loop().time()
        t.holder_id = await t.send("⠋ working…")
        t.pending_writes = [{"turn_seq": 0, "bubble_id": None, "is_initiator": True}]
        seq = t.turn_seq

        async def _noop():
            return None

        monkeypatch.setattr(t, "_sync_title", _noop)
        monkeypatch.setattr(t, "_jsonl", lambda: tmp_path / "absent.jsonl")
        monkeypatch.setattr(t, "_save", lambda: None)

        task = asyncio.create_task(t.kill_job(_ctx("tests")))
        await _pump_until(lambda: t.proc.stdin.writes and "stopping tests…" in ids.values())
        assert t.turn_seq == seq and len(t.pending_writes) == 1  # turn bookkeeping untouched
        _respond(t, t.proc.stdin.writes[0][1])
        await task

        final_card = t.holder_id
        await t._finalize_turn("all done")
        note_id = next(i for i, text in ids.items() if text == "stopping tests…")
        turn_messages = [i for i in ids if i not in c.bot.deleted and i != t.background_panel_id]
        assert note_id in turn_messages
        assert max(turn_messages) == final_card  # the finalized reply stays below the note
        assert t.background_panel_id > final_card  # the job panel sits below the turn

    asyncio.run(scenario())


def test_kill_without_a_live_cli_kills_the_job_tree(tmp_path, monkeypatch):
    async def scenario():
        c = TestClient(home=str(tmp_path))
        t = _topic(c, tmp_path, [("b1", "tests", None)])
        dead = _FakeProc(c.bot)
        dead.returncode = 0
        t.proc = dead
        calls = []

        async def fake_kill(path):
            calls.append(path)
            return 3

        monkeypatch.setattr(driver_module, "kill_file_holders", fake_kill)
        await t.kill_job(_ctx("tests"))
        assert calls == [str(tmp_path / "b1.output")]
        assert dead.stdin.writes == []
        assert c.replies[-1] == "stopping tests… (3 process(es))"
        assert t.background_tasks["b1"]["stopping"]

    asyncio.run(scenario())


def test_kill_fallback_refusal_is_reported(tmp_path, monkeypatch):
    async def scenario():
        c = TestClient(home=str(tmp_path))
        t = _topic(c, tmp_path, [("b1", "tests", None)])
        t.proc = None

        async def refuse(path):
            raise RuntimeError("reaches this process's own group")

        monkeypatch.setattr(driver_module, "kill_file_holders", refuse)
        await t.kill_job(_ctx("tests"))
        assert c.replies[-1] == "refused to stop tests: reaches this process's own group"

    asyncio.run(scenario())


def test_interrupt_still_writes_interrupt_and_leaves_no_waiter(tmp_path):
    async def scenario():
        c = TestClient(home=str(tmp_path))
        t = _topic(c, tmp_path, [])
        t.proc = _FakeProc(c.bot)
        await t._interrupt()
        assert [w[1]["request"] for w in t.proc.stdin.writes] == [{"subtype": "interrupt"}]
        assert t.cancel_requested is True
        assert t.control_waiters == {}

    asyncio.run(scenario())


def test_match_jobs_prefers_an_exact_label():
    running = {
        "bx1": {"label": "tests"},
        "bx2": {"label": "tests e2e"},
        "by3": {"label": "build"},
    }
    assert background.match_jobs(running, "TESTS") == ["bx1"]
    assert background.match_jobs(running, "tests e") == ["bx2"]
    assert background.match_jobs(running, "by") == ["by3"]  # an id prefix
    assert background.match_jobs(running, "zzz") == []
