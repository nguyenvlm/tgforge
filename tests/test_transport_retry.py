"""_call re-issues the request after a flood-wait via its factory, instead of
re-awaiting a spent coroutine (which used to raise RuntimeError and propagate); and it
never lets a flood-wait freeze a live turn — droppable calls skip, essential calls cap
the wait. Guards the panel-freeze fix."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from aiogram.exceptions import TelegramRetryAfter

from tgforge.base.kernel import _SKIPPED, FLOOD_WAIT_CAP, Transport


class _Flood(TelegramRetryAfter):
    def __init__(self, retry_after=0):
        self.retry_after = retry_after


def _flooding(n, retry_after=0):
    """A factory that raises flood-wait `n` times, then returns 'sent'."""
    state = {"left": n, "calls": 0}

    async def attempt():
        state["calls"] += 1
        if state["left"] > 0:
            state["left"] -= 1
            raise _Flood(retry_after)
        return "sent"

    return attempt, state


def test_call_reissues_after_flood_wait():
    async def scenario():
        t = Transport(bot=SimpleNamespace())
        attempt, state = _flooding(1)
        result = await t._call(lambda: attempt())
        assert result == "sent"  # the retry succeeded, no RuntimeError
        assert state["calls"] == 2  # a fresh coroutine on the second try

    asyncio.run(scenario())


def test_droppable_skips_without_sleeping(monkeypatch):
    async def scenario():
        slept = []
        real_sleep = asyncio.sleep
        monkeypatch.setattr(asyncio, "sleep", lambda s: slept.append(s) or real_sleep(0))
        t = Transport(bot=SimpleNamespace())
        attempt, state = _flooding(1, retry_after=300)
        out = await t._call(lambda: attempt(), droppable=True)
        assert out is _SKIPPED  # dropped, not delivered
        assert slept == []  # never blocks the reader
        assert state["calls"] == 1  # no retry — one attempt, then bail

    asyncio.run(scenario())


def test_essential_caps_the_sleep_then_retries(monkeypatch):
    async def scenario():
        slept = []
        real_sleep = asyncio.sleep
        monkeypatch.setattr(asyncio, "sleep", lambda s: slept.append(s) or real_sleep(0))
        t = Transport(bot=SimpleNamespace())
        attempt, _ = _flooding(1, retry_after=300)
        out = await t._call(lambda: attempt())
        assert out == "sent"
        assert slept == [FLOOD_WAIT_CAP]  # 300s capped to the ceiling

    asyncio.run(scenario())


def test_essential_gives_up_after_two_floods(monkeypatch):
    async def scenario():
        slept = []
        real_sleep = asyncio.sleep
        monkeypatch.setattr(asyncio, "sleep", lambda s: slept.append(s) or real_sleep(0))
        t = Transport(bot=SimpleNamespace())
        attempt, _ = _flooding(2, retry_after=300)
        out = await t._call(lambda: attempt())
        assert out is None  # one wait, one retry, then done — no unbounded loop
        assert slept == [FLOOD_WAIT_CAP]

    asyncio.run(scenario())
