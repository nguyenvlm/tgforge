"""The turn-boundary invariant: the finalized reply is the bottom-most message of the
turn — a mid-turn message the turn acknowledged stays ABOVE the finalized output, and a
message that arrives at/after the result boundary belongs to the next turn (the
finalized output stays above it). `_finalize_turn` takes the lock for the boundary and
queues the settle as a send step; every message of the window goes out through the
window's ordered send steps, so screen order is queue order.

"Below" == a higher message id (MockBot ids grow in chat order)."""

from __future__ import annotations

import asyncio

from claude_fakes import FakeProc, client, drain, replay, result, screen, texts, window


def _queued_ack(c, t):
    """A mid-turn message acknowledged by this turn (its "queued ✓" bubble)."""
    entry = {"is_initiator": False, "bubble_id": None, "reply_to": None, "turn_seq": t.turn_seq}
    t.pending_writes.append(entry)
    return entry


def test_finalized_output_lands_below_a_this_turn_midturn_message(tmp_path):
    """result-before-echo: the mid-turn message was acknowledged by this turn but its
    replay never arrived before the result; the turn's output goes below its ack."""

    async def scenario():
        c = client(tmp_path)
        t = window(c, tmp_path)
        t._open_holder()
        await drain(c, t)
        card = t.turn.card_id
        ack = await t.send("queued ✓")
        entry = _queued_ack(c, t)
        entry["bubble_id"] = ack
        t.proc = FakeProc([result("REPLYTOKEN")])
        await asyncio.wait_for(t._reader(), timeout=5)
        await drain(c, t)

        reply = next(mid for mid, text in screen(c) if "REPLYTOKEN" in text)
        assert reply > ack
        assert ack in c.bot.deleted  # the folded ack went
        assert card in c.bot.deleted  # the card above the ack was replaced below it

    asyncio.run(scenario())


def test_message_after_the_boundary_stays_below_the_finalized_output(tmp_path):
    async def scenario():
        c = client(tmp_path)
        t = window(c, tmp_path)
        t._open_holder()
        await drain(c, t)
        card = t.turn.card_id
        t.proc = FakeProc([result("REPLYTOKEN")])
        await asyncio.wait_for(t._reader(), timeout=5)
        await drain(c, t)
        await t.send("next turn's message")

        shown = texts(c)
        assert card not in c.bot.deleted  # no ack to sit below: the card became message 1
        assert shown.index("next turn's message") > next(
            i for i, text in enumerate(shown) if "REPLYTOKEN" in text
        )

    asyncio.run(scenario())


def test_finalize_keeps_busy_until_the_reader_settles(tmp_path):
    """busy is cleared by the reader atomically with the kill/keep decision, NOT in
    _finalize_turn, so a message right after the boundary queues instead of opening a
    second turn on a proc about to be killed."""

    async def scenario():
        c = client(tmp_path)
        t = window(c, tmp_path)
        t._open_holder()
        await t._finalize_turn("a reply")
        assert t.busy is True
        assert t.turn_open is False

    asyncio.run(scenario())


def test_late_echo_after_finalize_does_not_reopen_a_phantom_turn(tmp_path):
    async def scenario():
        c = client(tmp_path)
        t = window(c, tmp_path)
        t._open_holder()
        await drain(c, t)
        entry = _queued_ack(c, t)
        entry["bubble_id"] = await t.send("queued ✓")
        t.proc = FakeProc([result("REPLYTOKEN"), replay("ok")])
        await asyncio.wait_for(t._reader(), timeout=5)
        await drain(c, t)

        assert t.busy is False and t.turn_open is False
        assert not any("exited mid-turn" in text for text in texts(c))
        assert not t.pending_writes

    asyncio.run(scenario())


def test_next_turn_message_is_not_consumed_by_this_turn(tmp_path):
    async def scenario():
        c = client(tmp_path)
        t = window(c, tmp_path)
        t._open_holder()
        await drain(c, t)
        card = t.turn.card_id
        ack = await t.send("queued ✓")
        t.pending_writes = [
            {"is_initiator": False, "bubble_id": ack, "reply_to": None, "turn_seq": 1}
        ]
        await t._finalize_turn("REPLYTOKEN")
        await drain(c, t)

        assert card not in c.bot.deleted  # settled in place, above the next-turn message
        assert ack not in c.bot.deleted
        assert len(t.pending_writes) == 1  # survives for its own turn

    asyncio.run(scenario())


def test_a_mid_turn_fold_moves_the_card_below_the_message(tmp_path):
    """The replay of a mid-turn message folds it into the running turn: its ack goes and
    the card is re-sent below the user's message."""

    async def scenario():
        c = client(tmp_path)
        t = window(c, tmp_path)
        t._open_holder()
        await drain(c, t)
        old_card = t.turn.card_id
        user_msg = c.bot._id()
        c.bot.created.append((user_msg, 555, "also do this"))
        entry = _queued_ack(c, t)
        entry["bubble_id"] = await t.send("queued ✓")
        hold = asyncio.Event()
        t.proc = FakeProc([replay("first", "also do this"), hold])
        t.pending_writes.insert(0, {"is_initiator": True, "bubble_id": None, "reply_to": None})
        reader = asyncio.create_task(t._reader())
        await asyncio.sleep(0.05)
        await drain(c, t)

        assert old_card in c.bot.deleted and entry["bubble_id"] in c.bot.deleted
        assert t.turn.card_id > user_msg
        hold.set()
        await asyncio.wait_for(reader, 5)

    asyncio.run(scenario())
