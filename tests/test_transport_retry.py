"""_call re-issues the request after a flood-wait via its factory, instead of
re-awaiting a spent coroutine (which used to raise RuntimeError and propagate); an
essential call waits the full retry_after (never retried early) and retries until it
lands."""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)

from tgforge.base.kernel import TRANSIENT_BACKOFF, TRANSIENT_RETRIES, Transport


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


def _fake_clock(monkeypatch, start=1000.0):
    """A clock that advances only when the code sleeps; returns the recorded sleeps."""
    clock = {"t": start}
    monkeypatch.setattr("tgforge.base.kernel.time.monotonic", lambda: clock["t"])
    slept = []
    real_sleep = asyncio.sleep

    async def fake_sleep(seconds):
        slept.append(seconds)
        clock["t"] += seconds
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    return slept


def test_call_reissues_after_flood_wait():
    async def scenario():
        t = Transport(bot=SimpleNamespace())
        attempt, state = _flooding(1)
        result = await t._call(lambda: attempt())
        assert result == "sent"  # the retry succeeded, no RuntimeError
        assert state["calls"] == 2  # a fresh coroutine on the second try

    asyncio.run(scenario())


def test_essential_waits_the_full_retry_after_then_lands_once(monkeypatch):
    """A retry_after past the old 30 s cap is waited out in full: no early retry that
    fails again and loses the message after an extra API hit."""

    async def scenario():
        slept = _fake_clock(monkeypatch)
        t = Transport(bot=SimpleNamespace())
        attempt, state = _flooding(1, retry_after=45)
        out = await t._call(lambda: attempt())
        assert out == "sent"
        assert state["calls"] == 2  # one flooded attempt, one that landed
        assert slept == [45]  # the full 45 s in one wait, not a 30 s cap

    asyncio.run(scenario())


def test_essential_retries_until_it_lands(monkeypatch):
    async def scenario():
        _fake_clock(monkeypatch)
        t = Transport(bot=SimpleNamespace())
        attempt, state = _flooding(4, retry_after=1)
        assert await t._call(lambda: attempt()) == "sent"  # never given up on
        assert state["calls"] == 5

    asyncio.run(scenario())


def test_essential_calls_go_out_in_issue_order(monkeypatch):
    """A flood-waited call holds back the calls issued after it: they go out after it,
    in order, never ahead of it."""

    async def scenario():
        _fake_clock(monkeypatch)
        order = []
        floods = {"first": 1}

        async def send(tag):
            if floods.get(tag):
                floods[tag] -= 1
                raise _Flood(5)
            order.append(tag)
            return tag

        t = Transport(bot=SimpleNamespace())
        await asyncio.gather(*[t._call(lambda tag=tag: send(tag)) for tag in ("first", "second")])
        assert order == ["first", "second"]

    asyncio.run(scenario())


def test_edit_md_oversized_markdown_sends_full_plain_not_a_tail_cut():
    """An md render over MAX_MSG (MarkdownV2 escaping inflates a live panel past the
    limit) must never be hard-cut and sent — its final words would vanish mid-word.
    edit_md skips the oversized md and edits the caller-bounded plain in full."""
    from tgforge.base.kernel import MAX_MSG, Transport

    async def scenario():
        sent = []

        async def rec(text, **kw):
            sent.append(text)
            return SimpleNamespace(message_id=kw.get("message_id"))

        t = Transport(bot=SimpleNamespace(edit_message_text=rec))
        md = "*" + "x" * (MAX_MSG + 500) + " MD_TAIL"  # would be cut at MAX_MSG
        plain = "the reply body ends with PLAIN_TAIL_KEPT"
        landed = await t.edit_md(7, 42, md, plain)
        assert landed is True
        assert len(sent) == 1  # the oversized md attempt was skipped entirely
        assert sent[0] == plain  # full plain, tail intact
        assert "PLAIN_TAIL_KEPT" in sent[0]

    asyncio.run(scenario())


def test_flood_records_the_exact_deadline(monkeypatch):
    async def scenario():
        _fake_clock(monkeypatch)
        t = Transport(bot=SimpleNamespace())
        attempt, _ = _flooding(1, retry_after=300)
        assert await t._call(lambda: attempt()) == "sent"
        assert t.flood_until == 1300.0  # the exact retry_after deadline

    asyncio.run(scenario())


class _ServerError(TelegramServerError):
    def __init__(self):
        self.message = "Internal Server Error"

    def __str__(self):
        return self.message


class _NetworkError(TelegramNetworkError):
    def __init__(self):
        self.message = "connection reset"

    def __str__(self):
        return self.message


class _BadRequest(TelegramBadRequest):
    def __init__(self):
        self.message = "Bad Request: chat not found"

    def __str__(self):
        return self.message


def _failing(errors):
    """A factory raising each error in turn, then returning 'sent'."""
    state = {"calls": 0}

    async def attempt():
        state["calls"] += 1
        if errors:
            raise errors.pop(0)
        return "sent"

    return attempt, state


def test_a_5xx_or_network_error_is_retried_with_backoff(monkeypatch, caplog):
    import logging

    async def scenario():
        slept = _fake_clock(monkeypatch)
        t = Transport(bot=SimpleNamespace())
        attempt, state = _failing([_ServerError(), _NetworkError()])
        with caplog.at_level(logging.WARNING, logger="tgforge"):
            assert await t._call(lambda: attempt()) == "sent"
        assert state["calls"] == 3
        assert slept == [TRANSIENT_BACKOFF, 2 * TRANSIENT_BACKOFF]
        assert sum("retry in" in r.getMessage() for r in caplog.records) == 2

    asyncio.run(scenario())


def test_a_lasting_5xx_gives_up_after_the_bound(monkeypatch):
    async def scenario():
        _fake_clock(monkeypatch)
        t = Transport(bot=SimpleNamespace())
        attempt, state = _failing([_ServerError() for _ in range(10)])
        assert await t._call(lambda: attempt()) is None
        assert state["calls"] == TRANSIENT_RETRIES + 1

    asyncio.run(scenario())


def test_a_4xx_gets_one_try(monkeypatch):
    async def scenario():
        _fake_clock(monkeypatch)
        t = Transport(bot=SimpleNamespace())
        attempt, state = _failing([_BadRequest()])
        assert await t._call(lambda: attempt()) is None
        assert state["calls"] == 1

    asyncio.run(scenario())


def test_a_transient_retry_waits_out_a_flood_recorded_during_its_backoff(monkeypatch):
    async def scenario():
        _fake_clock(monkeypatch)
        t = Transport(bot=SimpleNamespace())
        tries = []

        async def attempt():
            tries.append(time.monotonic())
            if len(tries) == 1:
                t.flood_until = time.monotonic() + 20  # another call's flood, meanwhile
                raise _ServerError()
            return "sent"

        assert await t._call(lambda: attempt()) == "sent"
        assert tries[1] >= t.flood_until  # the retry waited the deadline out

    asyncio.run(scenario())


def test_a_transient_retry_takes_a_pacer_slot(monkeypatch):
    async def scenario():
        _fake_clock(monkeypatch)
        t = Transport(bot=SimpleNamespace(), pace_per_min=6, pace_burst=1)  # 10 s refill
        tries = []

        async def attempt():
            tries.append(time.monotonic())
            if len(tries) == 1:
                raise _ServerError()
            return "sent"

        assert await t._call(lambda: attempt()) == "sent"
        assert tries[1] - tries[0] >= 10 - 1e-6  # waited for a slot, not just the 1 s backoff

    asyncio.run(scenario())
