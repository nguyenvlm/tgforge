"""The turn-boundary invariant: the finalized reply is the bottom-most message of the
turn — a mid-turn message the turn acknowledged stays ABOVE the finalized output, and a
message that arrives at/after the result boundary belongs to the next turn (the
finalized output stays above it). `_finalize_turn` takes the lock for the boundary and
queues the settle as a send step; every message of the window goes out through the
window's ordered send steps, so screen order is queue order.

"Below" == a higher message id (MockBot ids grow in chat order)."""

from __future__ import annotations

import asyncio

from claude_fakes import (
    FakeProc,
    assistant_text,
    client,
    drain,
    replay,
    result,
    screen,
    texts,
    window,
)

from tgforge.plugins.claude.driver import UNDELIVERED_NOTE


def _queued_ack(c, t):
    """A mid-turn message acknowledged by this turn (its "queued ✓" bubble)."""
    entry = {"is_initiator": False, "bubble_id": None, "reply_to": None, "turn_seq": t.turn_seq}
    t.pending_writes.append(entry)
    return entry


def _user_message(c, text):
    """The user's own message in the thread (inbound, so the bot never sent it)."""
    mid = c.bot._id()
    c.bot.created.append((mid, 555, text))
    return mid


def _replies(c):
    """Message id → the message id it replies to, for every bot send."""
    real_send = c.bot.send_message
    replies = {}

    async def send(chat_id, text, **kw):
        sent = await real_send(chat_id, text, **kw)
        if kw.get("reply_parameters") is not None:
            replies[sent.message_id] = kw["reply_parameters"].message_id
        return sent

    c.bot.send_message = send
    return replies


async def _midturn_message(c, t, text="also do this"):
    """A message the user sends while the turn runs: their message, then its ack."""
    user_msg = _user_message(c, text)
    entry = _queued_ack(c, t)
    entry["reply_to"] = user_msg
    entry["bubble_id"] = await t.send("queued ✓", reply_to=user_msg)
    return user_msg, entry


def test_an_unreplayed_midturn_message_starts_the_next_turn(tmp_path):
    """The CLI runs a prompt written while it streams plain text as its own next turn:
    `result`, then the prompt's replay, then that turn. The process is kept and the ack
    stays until the replay opens the turn below the finalized reply."""

    async def scenario():
        c = client(tmp_path)
        t = window(c, tmp_path)
        replies = _replies(c)
        t._open_holder()
        await drain(c, t)
        card = t.turn.card_id
        user_msg, entry = await _midturn_message(c, t)
        ack = entry["bubble_id"]
        echo, second_result = asyncio.Event(), asyncio.Event()
        proc = FakeProc(
            [
                result("REPLYTOKEN"),
                echo,
                replay("also do this"),
                second_result,
                result("SECONDTOKEN"),
            ]
        )
        t.proc = proc
        reader = asyncio.create_task(t._reader())
        await asyncio.sleep(0.05)
        await drain(c, t)

        reply = next(mid for mid, text in screen(c) if "REPLYTOKEN" in text)
        assert proc.returncode is None  # kept: the queued prompt is still to run
        assert t.pending_writes == [entry] and t.busy
        assert ack not in c.bot.deleted  # still queued, its ack stays
        assert card in c.bot.deleted and reply > ack  # the output went below the ack

        echo.set()  # the CLI starts the queued turn
        await asyncio.sleep(0.05)
        await drain(c, t)
        assert ack in c.bot.deleted
        assert t.turn_open and t.turn.card_id > reply
        assert replies[t.turn.card_id] == user_msg  # the card replies to the user's message

        second_result.set()
        await asyncio.wait_for(reader, 5)
        await drain(c, t)
        second = next(mid for mid, text in screen(c) if "SECONDTOKEN" in text)
        assert second > reply
        assert proc.returncode == -9 and not t.pending_writes  # idle now: ended
        assert UNDELIVERED_NOTE not in texts(c)

    asyncio.run(scenario())


def test_a_queued_turn_with_no_replay_is_the_queued_message_turn(tmp_path):
    """A local slash command written late runs as the next turn with no replay
    (`assistant`, `result`): its first event opens the queued message's turn."""

    async def scenario():
        c = client(tmp_path)
        t = window(c, tmp_path)
        replies = _replies(c)
        t._open_holder()
        await drain(c, t)
        user_msg, entry = await _midturn_message(c, t, "/cost")
        proc = FakeProc([result("REPLYTOKEN"), assistant_text("cost: 1%"), result("COSTTOKEN")])
        t.proc = proc
        await asyncio.wait_for(t._reader(), timeout=5)
        await drain(c, t)

        reply = next(mid for mid, text in screen(c) if "REPLYTOKEN" in text)
        cost = next(mid for mid, text in screen(c) if "COSTTOKEN" in text)
        assert entry["bubble_id"] in c.bot.deleted
        assert cost > reply
        assert user_msg in replies.values()  # the slash turn's card replied to it
        assert proc.returncode == -9 and not t.pending_writes
        assert UNDELIVERED_NOTE not in texts(c)

    asyncio.run(scenario())


def test_a_replayed_midturn_message_folds_and_the_proc_ends(tmp_path):
    """A prompt merged at a tool boundary is replayed before the result: it folds into
    the turn (ack deleted) and is not kept past the result."""

    async def scenario():
        c = client(tmp_path)
        t = window(c, tmp_path)
        t._open_holder()
        await drain(c, t)
        _user_msg, entry = await _midturn_message(c, t)
        proc = FakeProc([replay("also do this"), result("REPLYTOKEN")])
        t.proc = proc
        await asyncio.wait_for(t._reader(), timeout=5)
        await drain(c, t)

        reply = next(mid for mid, text in screen(c) if "REPLYTOKEN" in text)
        assert entry["bubble_id"] in c.bot.deleted and reply > entry["bubble_id"]
        assert proc.returncode == -9 and not t.pending_writes
        assert UNDELIVERED_NOTE not in texts(c)

    asyncio.run(scenario())


def test_a_queued_message_whose_proc_dies_is_flagged_not_delivered(tmp_path):
    """The process exits with a queued message never run (this turn's, or one queued
    after the boundary): its ack is swapped for a notice replying to the message."""

    async def scenario():
        c = client(tmp_path)
        t = window(c, tmp_path)
        replies = _replies(c)
        t._open_holder()
        await drain(c, t)
        this_turn, entry = await _midturn_message(c, t)
        next_turn = _user_message(c, "after the boundary")
        later = {"is_initiator": False, "reply_to": next_turn, "turn_seq": t.turn_seq + 1}
        later["bubble_id"] = await t.send("queued ✓", reply_to=next_turn)
        t.pending_writes.append(later)
        t.proc = FakeProc([result("REPLYTOKEN")])  # then the stream ends: the CLI died
        await asyncio.wait_for(t._reader(), timeout=5)
        await drain(c, t)

        notes = [mid for mid, text in screen(c) if text == UNDELIVERED_NOTE]
        assert len(notes) == 2
        assert sorted(replies[mid] for mid in notes) == [this_turn, next_turn]
        assert entry["bubble_id"] in c.bot.deleted and later["bubble_id"] in c.bot.deleted
        assert not t.pending_writes and not t.busy

    asyncio.run(scenario())


def test_a_cancelled_reader_posts_no_undelivered_notice(tmp_path):
    """The window is closing (its reader cancelled): nothing is posted into it."""

    async def scenario():
        c = client(tmp_path)
        t = window(c, tmp_path)
        t._open_holder()
        await drain(c, t)
        await _midturn_message(c, t)
        t.proc = FakeProc([asyncio.Event()])
        reader = asyncio.create_task(t._reader())
        await asyncio.sleep(0.05)
        reader.cancel()
        await asyncio.gather(reader, return_exceptions=True)
        await drain(c, t)

        assert UNDELIVERED_NOTE not in texts(c)

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


def test_a_failed_card_move_keeps_the_old_card(tmp_path):
    """Only the re-send fails (Telegram rejects it); a delete still reaches the bot, so
    deleting the old card without a replacement would show."""
    from aiogram.exceptions import TelegramBadRequest

    async def scenario():
        c = client(tmp_path)
        t = window(c, tmp_path)
        t._open_holder()
        await drain(c, t)
        card = t.turn.card_id

        async def rejected(*args, **kwargs):
            raise TelegramBadRequest(method=None, message="Bad Request: rejected")

        c.bot.send_message = rejected
        await asyncio.wait_for(t._reorder_holder(), 2)
        assert t.turn.card_id == card  # still the live card
        assert card not in c.bot.deleted  # never deleted without a replacement

    asyncio.run(scenario())
