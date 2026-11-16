"""A "message is not modified" 400 on an edit is a no-op success, not a failure.
Without this, `edit_md`'s plain-text fallback fires on the benign 400 and rewrites
the message as plain text — flipping a monospace panel from shell to raw format
on every unchanged tick (the background panel flashing between raw and shell)."""

from __future__ import annotations

import asyncio

from aiogram.exceptions import TelegramBadRequest

from tgforge.testing import telegram_transport


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
    """Raises `exc` on every edit but a plain one (no rich body)."""

    def __init__(self, exc):
        self.exc = exc
        self.tried: list[str] = []  # the form of each edit tried, in order
        self.plain_edits: list[str] = []

    async def edit_message_text(self, text=None, **kw):
        if kw.get("rich_message") is not None:
            self.tried.append("rich")
            raise self.exc
        self.tried.append("plain")
        self.plain_edits.append(text)  # the last fallback
        return None


def test_not_modified_is_success_no_plain_fallback():
    async def scenario():
        bot = _Bot(_NotModified())
        tx = telegram_transport(bot)
        ok = await tx.edit_md(100, 7, "```\nshell\n```", "shell")
        assert ok is True  # treated as success
        assert bot.tried == ["rich"]  # no fallback edit → format stays shell

    asyncio.run(scenario())


def test_other_bad_request_still_falls_back_to_plain():
    async def scenario():
        bot = _Bot(_OtherBadRequest())
        tx = telegram_transport(bot)
        await tx.edit_md(100, 7, "```\nshell\n```", "shell")
        # rich, the plain body as escaped rich, then plain
        assert bot.tried == ["rich", "rich", "plain"]
        assert bot.plain_edits == ["shell"]  # a real markdown failure still degrades

    asyncio.run(scenario())


def test_a_live_message_edit_not_modified_tries_no_other_form():
    """A frame edit Telegram answers "message is not modified" (content that renders the
    same Rich HTML) has landed: no escaped Rich or plain retry flips its form."""

    async def scenario():
        bot = _Bot(_NotModified())
        tx = telegram_transport(bot, pace_per_min=0, pace_burst=0)
        assert await tx.land_frame("0", 7, "shell", "```\nshell\n```") is True
        assert bot.tried == ["rich"]
        tx.show_frame("0", 8, "shell 2", "```\nshell 2\n```")
        await tx.flush_frames()
        assert bot.tried == ["rich", "rich"] and bot.plain_edits == []

    asyncio.run(scenario())
