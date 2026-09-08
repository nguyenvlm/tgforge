"""The turn-boundary invariant: the finalized reply is the bottom-most message of the
turn — a mid-turn message the turn acknowledged stays ABOVE the finalized card, and a
message that arrives at/after the result boundary belongs to the next turn (card stays
above it). The race was: submit() acks+queues under self.lock while _finalize_turn()
settled the card in the reader task WITHOUT the lock, so a card could finalize above a
message this turn folded. _finalize_turn now takes the lock and reorders below any
this-turn mid-turn acks before settling.

Message order in MockBot: a later send() gets a higher auto-incrementing id, so
"below" == "higher id". The finalized card is delivered via edit_md, recorded in
bot.edits as (message_id, text)."""

from __future__ import annotations

import asyncio
import json

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
    def __init__(self, lines):
        self.returncode = None
        self.stdin = _FakeStdin()
        self.stdout = _FakeStdout(lines)

    def kill(self):
        self.returncode = -9


def _line(ev):
    return (json.dumps(ev) + "\n").encode()


def _ready(c, tmp_path):
    t = c.core._instantiate(ClaudeTopic, 555, "work")
    t.workspace = str(tmp_path)
    t._sync_title = lambda: asyncio.sleep(0)
    t._save = lambda: None
    t._jsonl = lambda: tmp_path / "none.jsonl"
    t.background_tasks = {}
    t.turn_start = asyncio.get_event_loop().time()
    return t


def _reply_edit_ids(c, token):
    return [mid for mid, text in c.bot.edits if token in text]


def test_finalized_card_lands_below_a_this_turn_midturn_message(tmp_path, monkeypatch):
    """result-before-echo: the mid-turn message is acknowledged by this turn but its
    replay echo never arrived before the result, so pending_writes still holds it at
    finalize. The card must reorder below the ack, not settle above it."""

    async def scenario():
        real_sleep = asyncio.sleep
        monkeypatch.setattr(asyncio, "sleep", lambda s: real_sleep(0))
        c = TestClient(home=str(tmp_path))
        t = _ready(c, tmp_path)

        t.busy = True
        t.holder_id = 42  # the live card, opened at turn start (low id → currently on top)
        # a mid-turn message ack'd during this turn: its "queued ✓" bubble sits BELOW the card
        ack_id = await t.send("queued ✓")
        assert ack_id > 42
        t.pending_writes = [{"is_initiator": False, "bubble_id": ack_id, "reply_to": None}]

        # the turn ends with a result and NO prior replay echo for the mid-turn message
        t.proc = _FakeProc(
            [_line({"type": "result", "result": "REPLYTOKEN", "subtype": "success"})]
        )
        await asyncio.wait_for(t._reader(), timeout=5)

        # invariant: the finalized reply is below the mid-turn ack (higher message id)
        ids = _reply_edit_ids(c, "REPLYTOKEN")
        assert ids, "the reply was never rendered into a card"
        assert max(ids) > ack_id, f"finalized card {ids} stranded above the mid-turn ack {ack_id}"

    asyncio.run(scenario())


def test_message_arriving_after_the_boundary_stays_below_the_finalized_card(tmp_path, monkeypatch):
    """no pending mid-turn ack at finalize → the card settles in place; a later message
    (next turn) is sent below it. The card must NOT reorder (nothing to sit below)."""

    async def scenario():
        real_sleep = asyncio.sleep
        monkeypatch.setattr(asyncio, "sleep", lambda s: real_sleep(0))
        c = TestClient(home=str(tmp_path))
        t = _ready(c, tmp_path)

        t.busy = True
        t.holder_id = 42
        t.pending_writes = []  # nothing acknowledged mid-turn

        t.proc = _FakeProc(
            [_line({"type": "result", "result": "REPLYTOKEN", "subtype": "success"})]
        )
        await asyncio.wait_for(t._reader(), timeout=5)

        # the card settled in place (id 42), no reorder send happened
        ids = _reply_edit_ids(c, "REPLYTOKEN")
        assert 42 in ids, "the reply should settle into the existing holder, not reorder"

    asyncio.run(scenario())


def test_finalize_keeps_busy_until_the_reader_settles(tmp_path, monkeypatch):
    """busy is cleared by the reader atomically with the kill/keep decision, NOT early in
    _finalize_turn. Otherwise a message during the reply-send (busy=False) would open a
    new-turn card mid-send instead of queuing; keeping busy True routes it to the queue."""

    async def scenario():
        real_sleep = asyncio.sleep
        monkeypatch.setattr(asyncio, "sleep", lambda s: real_sleep(0))
        c = TestClient(home=str(tmp_path))
        t = _ready(c, tmp_path)
        t.holder_id = 42
        t.busy = True

        await t._finalize_turn("a reply")

        assert t.busy is True  # finalize leaves busy for the reader to clear under the lock

    asyncio.run(scenario())


def test_reorder_deletes_the_old_card_essentially(tmp_path, monkeypatch):
    """A reorder sends the new card (droppable — skip the whole move under flood) then
    deletes the old. That delete must be essential, not droppable: a dropped delete
    strands the old card as a duplicate and the reorder reads as a no-op."""

    async def scenario():
        real_sleep = asyncio.sleep
        monkeypatch.setattr(asyncio, "sleep", lambda s: real_sleep(0))
        c = TestClient(home=str(tmp_path))
        t = _ready(c, tmp_path)
        t.holder_id = 42

        deletes = []

        async def spy_delete(mid, droppable=False):
            deletes.append((mid, droppable))

        monkeypatch.setattr(t, "delete", spy_delete)
        await t._reorder_holder()  # new card sends via MockBot (a fresh id), old = 42

        assert (42, False) in deletes, f"old card not deleted essentially: {deletes}"

    asyncio.run(scenario())


def test_late_echo_after_finalize_does_not_reopen_a_phantom_holder(tmp_path, monkeypatch):
    """The bug-2 root cause: pre-fix, a replay echo arriving AFTER finalize reopened a
    phantom live spinner on the mid-turn ack (then overwritten by a 'process exited'
    warning). With the fix, finalize consumes this-turn pending_writes, so the late echo
    finds nothing to reopen."""

    async def scenario():
        real_sleep = asyncio.sleep
        monkeypatch.setattr(asyncio, "sleep", lambda s: real_sleep(0))
        c = TestClient(home=str(tmp_path))
        t = _ready(c, tmp_path)

        t.busy = True
        t.holder_id = 42
        ack_id = await t.send("queued ✓")
        t.pending_writes = [{"is_initiator": False, "bubble_id": ack_id, "reply_to": None}]

        echo = {
            "type": "user",
            "message": {"role": "user", "content": [{"type": "text", "text": "ok"}]},
        }
        t.proc = _FakeProc(
            [
                _line({"type": "result", "result": "REPLYTOKEN", "subtype": "success"}),
                _line(echo),  # the late merged replay, arriving after finalize
            ]
        )
        await asyncio.wait_for(t._reader(), timeout=5)

        assert t.busy is False and t.holder_id is None  # clean, no phantom turn left open
        all_text = " ".join(text for _mid, text in c.bot.edits)
        assert "exited mid-turn" not in all_text  # no spurious interrupt warning
        assert not t.pending_writes  # the acknowledged message was consumed, not re-queued

    asyncio.run(scenario())


def test_next_turn_message_is_not_consumed_and_card_settles_in_place(tmp_path, monkeypatch):
    """A message stamped with a later turn_seq (ack'd at/after the boundary) belongs to
    the next turn: _finalize_turn must NOT consume it or reorder the card below it — the
    card settles in place (id 42), above it. Tested at the finalize level so the reader's
    end-of-stream cleanup (which discards queued writes on a dead proc) doesn't interfere."""

    async def scenario():
        real_sleep = asyncio.sleep
        monkeypatch.setattr(asyncio, "sleep", lambda s: real_sleep(0))
        c = TestClient(home=str(tmp_path))
        t = _ready(c, tmp_path)
        t.holder_id = 42
        t.turn_seq = 0
        ack_id = await t.send("queued ✓")
        # stamped for the NEXT turn (turn_seq 1 > this boundary 0)
        t.pending_writes = [
            {"is_initiator": False, "bubble_id": ack_id, "reply_to": None, "turn_seq": 1}
        ]

        await t._finalize_turn("REPLYTOKEN")

        ids = _reply_edit_ids(c, "REPLYTOKEN")
        assert 42 in ids, "the card should settle in place, not reorder below a next-turn message"
        assert len(t.pending_writes) == 1  # the next-turn message survives for its own turn

    asyncio.run(scenario())
