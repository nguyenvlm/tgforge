"""The panel-freeze fix's live behavior in the heartbeat loop itself: on sustained
flood the repaint interval backs off (so we stop hammering a closed per-message window),
and a tick with no new content is skipped (so a spinner tick alone never spends the
edit-rate budget). Complements test_flood_reader (reader drains) and test_transport_retry
(edit reports not-landed + records the deadline) by exercising `_heartbeat` end to end."""

from __future__ import annotations

import asyncio

from aiogram.exceptions import TelegramRetryAfter

from tgforge.plugins.claude import ClaudeTopic
from tgforge.plugins.claude import driver as drv
from tgforge.testing import TestClient


class _Flood(TelegramRetryAfter):
    def __init__(self, retry_after=120):
        self.retry_after = retry_after


def _live_topic(c, tmp_path):
    t = c.core._instantiate(ClaudeTopic, 555, "work")
    t.busy = True
    t.holder_id = 99
    t.turn_start = asyncio.get_event_loop().time()
    t.last_reader_event = t.turn_start
    return t


def test_heartbeat_backs_off_and_records_deadline_while_flooding(tmp_path, monkeypatch):
    async def scenario():
        monkeypatch.setattr(drv, "EDIT_INTERVAL", 0.01)  # tick fast
        c = TestClient(home=str(tmp_path))
        # every panel edit floods with a long retry_after
        monkeypatch.setattr(
            c.bot, "edit_message_text", lambda *a, **k: (_ for _ in ()).throw(_Flood(120))
        )
        t = _live_topic(c, tmp_path)
        t.heartbeat_task = asyncio.create_task(t._heartbeat())
        await asyncio.sleep(0.12)  # several flooding ticks
        await t._stop_heartbeat()

        # the interval grew past the base (multiplicative backoff), so repaints space out
        assert t._edit_interval > drv.EDIT_INTERVAL
        # the exact flood deadline was captured, so the loop waits out the closed window
        assert c.core.flood_until > asyncio.get_event_loop().time()

    asyncio.run(scenario())


def test_heartbeat_skips_noop_repaints(tmp_path, monkeypatch):
    async def scenario():
        monkeypatch.setattr(drv, "EDIT_INTERVAL", 0.01)  # many ticks in the window
        c = TestClient(home=str(tmp_path))
        t = _live_topic(c, tmp_path)  # empty turn: content never changes
        t.heartbeat_task = asyncio.create_task(t._heartbeat())
        await asyncio.sleep(0.1)  # ~10 ticks at 0.01s
        await t._stop_heartbeat()

        paints = [mid for mid, _ in c.bot.edits if mid == 99]
        # one paint lands; every later tick has an identical signature and is skipped
        # (the elapsed clock alone must not trigger an edit) — no per-tick spam
        assert len(paints) == 1

    asyncio.run(scenario())
