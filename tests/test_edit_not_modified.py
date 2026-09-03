"""A "message is not modified" 400 on an edit is a no-op success, not a failure.
Without this, `edit_md`'s plain-text fallback fires on the benign 400 and rewrites
the message with no parse_mode — flipping a monospace panel from shell to raw format
on every unchanged tick (the background panel flashing between raw and shell)."""

from __future__ import annotations

import asyncio

from aiogram.exceptions import TelegramBadRequest

from tgforge.base.kernel import Transport


class _NotModified(TelegramBadRequest):
    def __init__(self):
        pass  # str(self) → the class default carries the marker text below

    def __str__(self):
        return "Bad Request: message is not modified"


class _OtherBadRequest(TelegramBadRequest):
    def __init__(self):
        pass

    def __str__(self):
        return "Bad Request: message to edit not found"


class _Bot:
    def __init__(self, exc):
        self.exc = exc
        self.plain_edits: list[str] = []

    async def edit_message_text(self, text, **kw):
        if kw.get("parse_mode") == "MarkdownV2":
            raise self.exc
        self.plain_edits.append(text)  # the fallback path (no parse_mode)
        return None


def test_not_modified_is_success_no_plain_fallback():
    async def scenario():
        bot = _Bot(_NotModified())
        tx = Transport(bot)
        ok = await tx.edit_md(100, 7, "```\nshell\n```", "shell")
        assert ok is True  # treated as success
        assert bot.plain_edits == []  # never fell back to plain → format stays shell

    asyncio.run(scenario())


def test_other_bad_request_still_falls_back_to_plain():
    async def scenario():
        bot = _Bot(_OtherBadRequest())
        tx = Transport(bot)
        await tx.edit_md(100, 7, "```\nshell\n```", "shell")
        assert bot.plain_edits == ["shell"]  # a real markdown failure still degrades

    asyncio.run(scenario())
