"""The '/cancel' bubble must never stick at 'cancelling…'. It is settled to
'✖️ cancelled' at every turn-end boundary — the finalize path (a cancelled result) and
the reader teardown (a cancel that ended via a dead proc) — via _settle_cancel_note."""

from __future__ import annotations

import asyncio

from claude_fakes import FakeProc, client, drain, window


def test_cancel_bubble_is_settled_when_the_turn_finalizes(tmp_path):
    async def scenario():
        c = client(tmp_path)
        t = window(c, tmp_path)
        t._open_holder()
        t.proc = FakeProc()
        await drain(c, t)

        await t.cancel(None)  # sends "cancelling…", remembers its id
        assert t.cancel_msg_id is not None
        note_id = t.cancel_msg_id

        await t._finalize_turn("done")
        await drain(c, t)

        assert t.cancel_msg_id is None  # settled, not left dangling
        assert any(mid == note_id and "cancelled" in text for mid, text in c.bot.edits)

    asyncio.run(scenario())


def test_cancel_bubble_is_settled_when_the_proc_dies(tmp_path):
    """No result event — the turn ends via the reader teardown. The bubble is still
    settled (the shared boundary catches it), never left at 'cancelling…'."""

    async def scenario():
        c = client(tmp_path)
        t = window(c, tmp_path)
        t.cancel_msg_id = await t.send("cancelling...")  # a cancel was requested earlier
        note_id = t.cancel_msg_id
        t.proc = FakeProc([])  # empty stream → reader exits into its finally teardown

        await asyncio.wait_for(t._reader(), timeout=5)

        assert t.cancel_msg_id is None
        assert any(mid == note_id and "cancelled" in text for mid, text in c.bot.edits)

    asyncio.run(scenario())
