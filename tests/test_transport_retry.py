"""_call re-issues the request after a flood-wait via its factory, instead of
re-awaiting a spent coroutine (which used to raise RuntimeError and propagate)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from aiogram.exceptions import TelegramRetryAfter

from tgforge.base.kernel import Transport


class _Flood(TelegramRetryAfter):
    def __init__(self):
        self.retry_after = 0  # skip the real sleep


def test_call_reissues_after_flood_wait():
    async def scenario():
        t = Transport(bot=SimpleNamespace())
        n = {"calls": 0}

        async def attempt():
            n["calls"] += 1
            if n["calls"] == 1:
                raise _Flood()
            return "sent"

        result = await t._call(lambda: attempt())
        assert result == "sent"  # the retry succeeded, no RuntimeError
        assert n["calls"] == 2  # a fresh coroutine on the second try

    asyncio.run(scenario())
