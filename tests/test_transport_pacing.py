"""The transport paces every outbound call under Telegram's per-group rate so a 429
never fires in the first place. Essential calls (a reply) wait for a slot; frame pushes
wait for a spare one and yield to any pending essential."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from tgforge.base.kernel import _Pacer
from tgforge.testing import rich_text, telegram_transport


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

        async def rec(text=None, **kw):
            sent.append(rich_text(kw["rich_message"].html) if "rich_message" in kw else text)
            return SimpleNamespace(message_id=1)

        bot = SimpleNamespace(edit_message_text=rec)
        t = telegram_transport(bot, pace_per_min=60, pace_burst=1)
        t._pacer.tokens = 0  # empty → an essential edit must wait a refill
        landed = await t.edit("0", 42, "the reply")
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
    t = telegram_transport(SimpleNamespace())
    assert t._pacer.enabled is False  # off by default so tests never pace
    t.enable_pacing()
    assert t._pacer.enabled is True  # the live run turns it on


def test_a_live_message_fallback_takes_its_own_pacer_slot(monkeypatch):
    from aiogram.exceptions import TelegramBadRequest

    class _Refused(TelegramBadRequest):
        def __init__(self):
            self.message = "Bad Request: can't parse rich message"

    async def scenario():
        clock, _ = _fake_clock(monkeypatch)
        edits = []  # (form, clock) per edit attempt

        async def edit_message_text(text=None, **kw):
            rich = "rich_message" in kw
            escaped = rich and kw["rich_message"].html == "<p>x</p>"
            edits.append(("escaped Rich" if escaped else "rich" if rich else "plain", clock["t"]))
            if rich and not escaped:
                raise _Refused()
            return SimpleNamespace(message_id=kw["message_id"])

        bot = SimpleNamespace(edit_message_text=edit_message_text)
        t = telegram_transport(bot, pace_per_min=60, pace_burst=1)  # 1 slot/s, 1 at once
        assert await t.land_frame("0", 42, "x", "**x**")
        assert [form for form, _ in edits] == ["rich", "escaped Rich"]
        # the refused rich edit spent the one slot: the fallback waits a refill
        assert edits[1][1] - edits[0][1] >= 1.0

    asyncio.run(scenario())
