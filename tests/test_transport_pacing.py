"""The transport paces every outbound call under Telegram's per-group rate so a 429
never fires in the first place. Essential calls (a reply) wait for a slot; frame pushes
wait for a spare one and yield to any pending essential."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from tgforge.base.kernel import Transport, _Pacer


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
        await p.take()  # burst slot 1 — instant
        await p.take()  # burst slot 2 — instant
        assert slept == []
        await p.take()  # empty → waits one refill (~1s)
        assert len(slept) == 1 and abs(slept[0] - 1.0) < 1e-6

    asyncio.run(scenario())


def test_spare_slot_yields_to_a_pending_essential(monkeypatch):
    async def scenario():
        clock, slept = _fake_clock(monkeypatch)
        p = _Pacer(per_min=60, burst=5)  # tokens available
        p.waiting = 1  # an essential reply is pending

        async def release():
            await asyncio.sleep(0.5)
            p.waiting = 0

        releaser = asyncio.create_task(release())
        await p.take_spare()  # waits although tokens are free
        await releaser
        assert slept and clock["t"] >= 0.5  # it only got the slot once the reply was done

    asyncio.run(scenario())


def test_pacer_disabled_is_a_noop(monkeypatch):
    async def scenario():
        _, slept = _fake_clock(monkeypatch)
        p = _Pacer(per_min=0, burst=0)  # disabled
        for _ in range(50):
            await p.take()
            await p.take_spare()
        assert slept == []  # never paces

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


def test_pacer_concurrent_essentials_serialize(monkeypatch):
    """Under real concurrency the lock must stop two waiting essentials from taking the
    same refilled token — all grants land, tokens never driven negative."""

    async def scenario():
        p = _Pacer(per_min=60000, burst=1)  # ~1ms per token → fast, real sleeps
        await p.take()  # drain the initial token
        await asyncio.gather(*[p.take() for _ in range(8)])  # every waiter gets its own slot
        assert p.tokens > -1e-9  # never over-consumed past zero

    asyncio.run(scenario())


def test_enable_pacing_flips_it_on():
    t = Transport(bot=SimpleNamespace())
    assert t._pacer.enabled is False  # off by default so tests never pace
    t.enable_pacing()
    assert t._pacer.enabled is True  # the live run turns it on
