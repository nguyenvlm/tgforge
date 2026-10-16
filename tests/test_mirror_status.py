"""The mirror loop only reads the transcript: its spinner is a frame and its messages
go out as send steps, so a Telegram flood-wait never blocks it."""

from __future__ import annotations

import asyncio
import json
import time

from aiogram.exceptions import TelegramRetryAfter
from claude_fakes import client, drain, texts, window


class _Flood(TelegramRetryAfter):
    def __init__(self, retry_after=300):
        self.retry_after = retry_after


def test_the_mirror_spinner_never_blocks_on_a_flood(tmp_path):
    async def scenario():
        c = client(tmp_path)

        async def flood(*a, **k):
            raise _Flood(300)

        c.bot.edit_message_text = flood
        t = window(c, tmp_path)
        t.mirror_holder = 77  # holder already exists → straight to the spinner frame
        t.mirror_start = time.time()
        started = time.monotonic()
        painted = t._mirror_status(["Read"], in_flight=True)  # records, returns
        if asyncio.iscoroutine(painted):  # holds on an implementation that awaits here
            await asyncio.wait_for(painted, 5)
        assert time.monotonic() - started < 0.1

    asyncio.run(scenario())


def test_mirrored_texts_are_sent_by_send_steps_in_order(tmp_path):
    async def scenario():
        c = client(tmp_path)
        t = window(c, tmp_path)
        jsonl = tmp_path / "session.jsonl"
        t._jsonl = lambda: jsonl
        jsonl.write_text("")
        t.mirror_offset = 0
        release = asyncio.Event()
        real_send = c.bot.send_message

        async def slow_send(chat_id, text, **kw):
            await release.wait()
            return await real_send(chat_id, text, **kw)

        c.bot.send_message = slow_send
        lines = [
            {"type": "user", "message": {"content": "first from the PC"}},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "an answer"}]}},
        ]
        jsonl.write_text("".join(json.dumps(ev) + "\n" for ev in lines))
        assert await asyncio.wait_for(t._mirror_once(), 1)  # returns while sends are stuck
        release.set()
        await drain(c, t)
        shown = [text.replace("\\", "") for text in texts(c)]
        assert shown == ["[user] first from the PC", "[cli] an answer"]

    asyncio.run(scenario())
