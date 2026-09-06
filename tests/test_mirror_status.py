"""The mirror spinner is cosmetic and droppable: a Telegram flood-wait on the
"working…" holder edit must not block the mirror loop (which also delivers the real
mirrored messages), so `_mirror_status` returns at once instead of sleeping the flood cap."""

from __future__ import annotations

import asyncio

from aiogram.exceptions import TelegramRetryAfter

from tgforge.plugins.claude import ClaudeTopic
from tgforge.testing import TestClient


class _Flood(TelegramRetryAfter):
    def __init__(self, retry_after=300):
        self.retry_after = retry_after


def test_mirror_spinner_edit_is_droppable(tmp_path, monkeypatch):
    async def scenario():
        slept = []
        real_sleep = asyncio.sleep
        monkeypatch.setattr(asyncio, "sleep", lambda s: slept.append(s) or real_sleep(0))
        c = TestClient(home=str(tmp_path))
        # the spinner holder edit floods
        monkeypatch.setattr(
            c.bot, "edit_message_text", lambda *a, **k: (_ for _ in ()).throw(_Flood(300))
        )
        t = c.core._instantiate(ClaudeTopic, 555, "work")
        t.mirror_holder = 77  # holder already exists → straight to the spinner edit
        t.mirror_start = asyncio.get_event_loop().time()

        await t._mirror_status(["Read"], in_flight=True)  # must not raise or block

        assert slept == []  # dropped, not the 300s→cap sleep that stalls the loop

    asyncio.run(scenario())
