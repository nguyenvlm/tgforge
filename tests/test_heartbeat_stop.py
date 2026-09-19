"""Single-writer discipline for the holder message: the reader owns it and must
quiesce the one live periodic painter (heartbeat while busy, background-panel updater
after) before it paints — else an in-flight timer edit lands after the reader's write
and reverts the card to a running frame (`❄ …/💬 T`) or a stale id."""

from __future__ import annotations

import asyncio

from tgforge.plugins.claude import ClaudeTopic
from tgforge.plugins.claude import driver as drv
from tgforge.testing import TestClient


def test_stop_heartbeat_halts_repaint(tmp_path, monkeypatch):
    async def scenario():
        monkeypatch.setattr(drv, "EDIT_INTERVAL", 0.01)
        c = TestClient(home=str(tmp_path))
        t = c.core._instantiate(ClaudeTopic, 555, "work")
        t.busy = True
        t.holder_id = 99
        t.turn_start = asyncio.get_event_loop().time()
        t.heartbeat_task = asyncio.create_task(t._heartbeat())
        await asyncio.sleep(0.05)  # let it tick a few times
        assert any(mid == 99 for mid, _ in c.bot.edits)  # heartbeat is repainting

        await t._stop_heartbeat()
        assert t.heartbeat_task is None
        seen = len(c.bot.edits)
        await asyncio.sleep(0.05)  # no tick may land after the stop
        assert len(c.bot.edits) == seen  # the holder is frozen at the final card

    asyncio.run(scenario())


def test_finalize_stops_heartbeat_before_editing(tmp_path):
    async def scenario():
        c = TestClient(home=str(tmp_path))
        t = c.core._instantiate(ClaudeTopic, 555, "work")
        t.holder_id = 99
        order: list[str] = []

        async def rec_stop():
            order.append("stop")

        async def rec_edit(*a, **k):
            order.append("edit")
            return True

        t._stop_heartbeat = rec_stop
        t.edit_md = rec_edit
        t._sync_title = lambda: asyncio.sleep(0)
        t._save = lambda: None
        t._jsonl = lambda: tmp_path / "none.jsonl"
        await t._finalize_turn("done")
        assert order and order[0] == "stop"  # heartbeat halted before the first edit

    asyncio.run(scenario())


def test_open_holder_revives_dead_heartbeat_on_reused_proc(tmp_path, monkeypatch):
    """A background-task completion opens the next turn straight through the reader's
    `_open_holder` — it never calls `_ensure_proc`. The prior turn's settle cancelled
    the heartbeat, so `_open_holder` must revive it, or the live panel freezes at its
    first frame (`… 0s · ↓ 0 tokens`) with no intermediate updates before the result."""

    async def scenario():
        monkeypatch.setattr(drv, "EDIT_INTERVAL", 0.01)
        c = TestClient(home=str(tmp_path))
        t = c.core._instantiate(ClaudeTopic, 555, "work")

        class _LiveProc:
            returncode = None

        t.proc = _LiveProc()  # kept alive across turns by a background task
        t.heartbeat_task = None  # cancelled by the prior turn's _finalize_turn
        await t._open_holder()  # background-completion path: must revive the painter
        assert t.heartbeat_task is not None and not t.heartbeat_task.done()

        await asyncio.sleep(0.05)
        assert any(mid == t.holder_id for mid, _ in c.bot.edits)  # this turn actually paints
        await t._stop_heartbeat()
        t.proc = None

    asyncio.run(scenario())


def test_open_holder_keeps_a_live_heartbeat_singleton(tmp_path):
    """Opening a turn whose heartbeat is still alive must not spawn a second painter."""

    async def scenario():
        c = TestClient(home=str(tmp_path))
        t = c.core._instantiate(ClaudeTopic, 555, "work")

        async def _idle():
            await asyncio.sleep(3600)

        live = asyncio.create_task(_idle())
        t.heartbeat_task = live
        await t._open_holder()
        assert t.heartbeat_task is live  # same task, not duplicated
        live.cancel()

    asyncio.run(scenario())


def test_open_holder_quiesces_updater_at_turn_open(tmp_path):
    async def scenario():
        c = TestClient(home=str(tmp_path))
        t = c.core._instantiate(ClaudeTopic, 555, "work")
        t.background_panel_id = 88  # a live panel message from a between-turn job
        stopped: list[str] = []

        async def rec_stop():
            stopped.append("stop")

        t._stop_background_updater = rec_stop
        await t._open_holder()
        if t.heartbeat_task is not None:
            t.heartbeat_task.cancel()
        # the between-turn panel updater is quiesced at turn open so the heartbeat is the
        # sole painter of the panel message; the panel message itself is left in place
        # (its job may still be running) — never settled or retired at the turn boundary
        assert stopped == ["stop"]
        assert t.background_panel_id == 88

    asyncio.run(scenario())
