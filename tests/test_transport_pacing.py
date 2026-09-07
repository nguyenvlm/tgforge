"""The transport paces every outbound call under Telegram's per-group rate so a 429
never fires in the first place. Essential calls (a reply) wait for a slot; droppable
repaints skip when none is free and yield to any waiting essential."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from tgforge.base.kernel import _SKIPPED, Transport, _Pacer


def _fake_clock(monkeypatch):
    clock = {"t": 0.0}
    monkeypatch.setattr("tgforge.base.kernel.time.monotonic", lambda: clock["t"])
    slept = []
    real_sleep = asyncio.sleep

    async def fake_sleep(s):
        slept.append(s)
        clock["t"] += s  # advance the clock as if the wait elapsed
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    return clock, slept


def test_pacer_bursts_then_paces_essential(monkeypatch):
    async def scenario():
        _, slept = _fake_clock(monkeypatch)
        p = _Pacer(per_min=60, burst=2)  # 1 token/s, burst of 2
        assert await p.acquire(wait=True)  # burst slot 1 — instant
        assert await p.acquire(wait=True)  # burst slot 2 — instant
        assert slept == []
        assert await p.acquire(wait=True)  # empty → waits one refill (~1s)
        assert len(slept) == 1 and abs(slept[0] - 1.0) < 1e-6

    asyncio.run(scenario())


def test_pacer_droppable_skips_when_empty(monkeypatch):
    async def scenario():
        _fake_clock(monkeypatch)
        p = _Pacer(per_min=60, burst=1)
        assert await p.acquire(wait=True)  # drains the one token
        assert await p.acquire(wait=False) is False  # droppable skips, never blocks

    asyncio.run(scenario())


def test_pacer_droppable_yields_to_a_waiting_essential(monkeypatch):
    async def scenario():
        _fake_clock(monkeypatch)
        p = _Pacer(per_min=60, burst=5)  # tokens available
        p.waiting = 1  # an essential reply is queued for a slot
        assert await p.acquire(wait=False) is False  # repaint yields even with tokens free
        p.waiting = 0
        assert await p.acquire(wait=False) is True  # no contender → takes a token

    asyncio.run(scenario())


def test_pacer_disabled_is_a_noop(monkeypatch):
    async def scenario():
        _fake_clock(monkeypatch)
        p = _Pacer(per_min=0, burst=0)  # disabled
        for _ in range(50):
            assert await p.acquire(wait=False) is True  # never paces, never skips

    asyncio.run(scenario())


def test_call_droppable_paced_out_skips_the_bot(monkeypatch):
    async def scenario():
        _fake_clock(monkeypatch)
        calls = []
        bot = SimpleNamespace(edit_message_text=lambda *a, **k: calls.append(1))
        t = Transport(bot=bot, pace_per_min=60, pace_burst=1)
        t._pacer.tokens = 0  # budget spent
        landed = await t.edit(7, 42, "spinner tick", droppable=True)
        assert landed is False  # paced out → _SKIPPED → not landed
        assert calls == []  # the bot was never called — no 429 provoked

    asyncio.run(scenario())


def test_call_essential_waits_for_a_slot_then_sends(monkeypatch):
    async def scenario():
        _, slept = _fake_clock(monkeypatch)
        sent = []

        async def rec(text, **kw):
            sent.append(text)
            return SimpleNamespace(message_id=1)

        t = Transport(bot=SimpleNamespace(edit_message_text=rec), pace_per_min=60, pace_burst=1)
        t._pacer.tokens = 0  # empty → an essential edit must wait a refill
        landed = await t.edit(7, 42, "the reply")
        assert landed is True
        assert sent == ["the reply"]  # delivered, not dropped
        assert slept and slept[0] > 0  # it paced instead of racing to a 429

    asyncio.run(scenario())


def test_pacer_returns_skipped_sentinel_directly(monkeypatch):
    async def scenario():
        _fake_clock(monkeypatch)
        t = Transport(bot=SimpleNamespace(), pace_per_min=60, pace_burst=1)
        t._pacer.tokens = 0
        out = await t._call(lambda: None, droppable=True)
        assert out is _SKIPPED

    asyncio.run(scenario())


def test_pacer_concurrent_essentials_serialize(monkeypatch):
    """Under real concurrency the lock must stop two waiting essentials from taking the
    same refilled token — all grants land, tokens never driven negative."""

    async def scenario():
        p = _Pacer(per_min=60000, burst=1)  # ~1ms per token → fast, real sleeps
        assert await p.acquire(wait=True)  # drain the initial token
        results = await asyncio.gather(*[p.acquire(wait=True) for _ in range(8)])
        assert results == [True] * 8  # every waiter eventually got its own slot
        assert p.tokens > -1e-9  # never over-consumed past zero

    asyncio.run(scenario())


def test_enable_pacing_flips_it_on():
    t = Transport(bot=SimpleNamespace())
    assert t._pacer.enabled is False  # off by default so tests never pace
    t.enable_pacing()
    assert t._pacer.enabled is True  # the live run turns it on
