"""Telegram's reply to a Rich Message holding an expandable quote (every collapsed
tool-call page) parses in aiogram's model, so a card edit lands and no final answer is
held back behind it; a frame push that raises still resolves its waiters, and a send
step stuck behind the one before it is logged."""

from __future__ import annotations

import asyncio
import logging
import time

from aiogram.types import Message
from claude_fakes import FakeProc, assistant_text, client, drain, result, texts, window

from tgforge.testing import MockBot

# Telegram's reply to a card edit (a journal's logged reply, its names made neutral): a
# paragraph, then an expandable quote whose text holds links
_EDITED_CARD = {
    "message_id": 101,
    "from": {"id": 42, "is_bot": True, "first_name": "mybot", "username": "mybot"},
    "chat": {"id": -1001, "title": "Group", "is_forum": True, "type": "supergroup"},
    "date": 1700000000,
    "edit_date": 1700000100,
    "message_thread_id": 7,
    "rich_message": {
        "blocks": [
            {"type": "paragraph", "text": "🔧 2 · 1m 3s · ↓ 9.5k"},
            {
                "type": "expandable_blockquote",
                "text": [
                    "💻 Bash: ls\n\n📖 Read: ",
                    {"type": "url", "text": "notes.md", "url": "notes.md"},
                    "\n\n💻 Bash: done",
                ],
            },
        ]
    },
    "is_topic_message": True,
}


def test_aiograms_model_reads_a_rich_message_holding_an_expandable_quote():
    message = Message.model_validate(_EDITED_CARD)  # aiogram 3.30 raised ValidationError
    assert message.message_id == 101
    assert [block.type for block in message.rich_message.blocks] == [
        "paragraph",
        "expandable_blockquote",
    ]


def _reply(message_id: int, thread: int | None, html: str) -> Message:
    """Telegram's reply to a Rich send or edit, read through aiogram's real model."""
    reply = dict(_EDITED_CARD, message_id=message_id, message_thread_id=thread)
    if "<blockquote expandable>" not in html:
        reply["rich_message"] = {"blocks": [{"type": "paragraph", "text": "text"}]}
    return Message.model_validate(reply)


class _TelegramReplyBot(MockBot):
    """MockBot whose Rich sends and edits answer as Telegram does, through aiogram's
    `Message` model (a reply it cannot read raises, as aiogram's session does)."""

    async def send_rich_message(self, chat_id, rich_message, **kw):
        sent = await super().send_rich_message(chat_id, rich_message, **kw)
        return _reply(sent.message_id, kw.get("message_thread_id"), rich_message.html)

    async def edit_message_text(self, text=None, **kw):
        rich = kw.get("rich_message")
        edited = await super().edit_message_text(text, **kw)
        if rich is None:
            return edited
        return _reply(kw.get("message_id"), kw.get("message_thread_id"), rich.html)


def _with_live_panel(t, tmp_path):
    out = tmp_path / "job.output"
    out.write_text("working\n")
    t.background_tasks = {
        "b1": {"path": str(out), "label": "a job", "start": time.monotonic(), "done": None}
    }
    t._paint_panel(0)


def test_every_final_answer_lands_after_a_folded_card(tmp_path):
    """The live journal's sequence: a live background-jobs panel; a turn whose card folds
    its tool calls into an expandable quote; a turn opened by a background-task
    completion; a normal turn. Each answer is on screen once."""

    async def scenario():
        c = client(tmp_path)
        c.bot = _TelegramReplyBot()
        c.core.transports[0].adapter.bot = c.bot
        t = window(c, tmp_path)
        _with_live_panel(t, tmp_path)
        await drain(c, t)
        t._open_holder()
        await drain(c, t)
        t.turn.timeline.append(("tool", "💻 Bash: ls"))
        await t._finalize_turn("ANSWER ONE")
        await drain(c, t)
        t.proc = FakeProc([assistant_text("x"), result("ANSWER TWO")])
        t.pending_writes = []
        await asyncio.wait_for(t._reader(), 5)
        await drain(c, t)
        t._open_holder()
        await drain(c, t)
        await t._finalize_turn("ANSWER THREE")
        await asyncio.wait_for(drain(c, t), 5)
        return texts(c)

    shown = asyncio.run(asyncio.wait_for(scenario(), 15))
    for answer in ("ANSWER ONE", "ANSWER TWO", "ANSWER THREE"):
        assert sum(answer in text for text in shown) == 1, (answer, shown)


def test_a_frame_push_that_raises_resolves_its_land_frame_as_not_landed(caplog):
    from types import SimpleNamespace

    from tgforge.testing import telegram_transport

    async def edit_message_text(*args, **kw):
        raise RuntimeError("the reply could not be read")

    async def scenario():
        tx = telegram_transport(SimpleNamespace(edit_message_text=edit_message_text))
        return await asyncio.wait_for(tx.land_frame("7", 50, "plain"), 5)

    with caplog.at_level(logging.ERROR, logger="tgforge"):
        assert asyncio.run(scenario()) is False  # never a hang the window waits on forever
    assert any(
        r.levelno == logging.ERROR and "frame push failed" in r.getMessage() for r in caplog.records
    )


def test_a_send_step_stuck_behind_the_one_before_it_is_logged(tmp_path, monkeypatch, caplog):
    from tgforge.plugins.agent import window as drv

    monkeypatch.setattr(drv, "SEND_STEP_STALL", 0.05)

    async def scenario():
        c = client(tmp_path)
        t = window(c, tmp_path)
        release = asyncio.Event()
        ran = []
        t._send_in_order(release.wait)

        async def later():
            ran.append("later")

        with caplog.at_level(logging.WARNING, logger="tgforge"):
            step = t._send_in_order(later)
            await asyncio.sleep(0.15)
            assert ran == []  # order kept: it still waits
            release.set()
            await step
        return ran

    assert asyncio.run(scenario()) == ["later"]
    assert any("waits over" in r.getMessage() for r in caplog.records)
