"""The background-job panel updater shares the heartbeat's flood discipline: its 5s
metronome would flood-freeze a long job's panel the same way. Under sustained flood its
droppable edit records the deadline and the loop honors it (waits out the window) instead
of hammering — proven by driving `_background_updater` while every edit floods."""

from __future__ import annotations

import asyncio

from aiogram.exceptions import TelegramRetryAfter

from tgforge.plugins.claude import ClaudeTopic
from tgforge.plugins.claude import driver as drv
from tgforge.testing import TestClient


class _Flood(TelegramRetryAfter):
    def __init__(self, retry_after=300):
        self.retry_after = retry_after


def test_background_updater_honors_flood_deadline(tmp_path, monkeypatch):
    async def scenario():
        slept = []
        real_sleep = asyncio.sleep
        monkeypatch.setattr(asyncio, "sleep", lambda s: slept.append(s) or real_sleep(0))
        monkeypatch.setattr(drv, "UPDATER_INTERVAL", 0.01)  # tick fast
        c = TestClient(home=str(tmp_path))
        monkeypatch.setattr(
            c.bot, "edit_message_text", lambda *a, **k: (_ for _ in ()).throw(_Flood(300))
        )
        t = c.core._instantiate(ClaudeTopic, 555, "work")
        t.last_final_id = 88
        t.last_final_body = ("m", "p")
        t.last_final_markup = None
        now = asyncio.get_event_loop().time()
        t.background_tasks = {"j": {"done": None, "start": now, "label": "job", "path": "/no/such"}}

        task = asyncio.create_task(t._background_updater())
        await real_sleep(0.05)  # let it flood a few ticks
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        assert c.core.flood_until > 0  # the droppable edit recorded the exact deadline
        # a later tick waited out that window (slack ≈ 300s) instead of the 0.01s metronome
        assert any(s > drv.UPDATER_INTERVAL * 10 for s in slept)

    asyncio.run(scenario())
