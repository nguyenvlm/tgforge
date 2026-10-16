"""The live-card painter: one per window, armed at every turn open (`_open_holder`, the
boundary all turn-entry paths share), painting only the open turn's card, and stopped
cleanly. Its frames go through the kernel's frame-skip output."""

from __future__ import annotations

import asyncio

from claude_fakes import client, drain, window

from tgforge.plugins.claude import driver as drv


def test_stop_heartbeat_halts_repaint(tmp_path, monkeypatch):
    async def scenario():
        monkeypatch.setattr(drv, "EDIT_INTERVAL", 0.01)
        c = client(tmp_path)
        t = window(c, tmp_path)
        t._open_holder()
        await asyncio.sleep(0.05)  # let it tick a few times
        await drain(c, t)
        card = t.turn.card_id
        assert any(mid == card for mid, _ in c.bot.edits)  # the heartbeat is repainting

        await t._stop_heartbeat()
        assert t.heartbeat_task is None
        seen = len(c.bot.edits)
        await asyncio.sleep(0.05)
        await drain(c, t)
        assert len(c.bot.edits) == seen

    asyncio.run(scenario())


def test_open_holder_revives_a_dead_heartbeat_on_a_reused_proc(tmp_path, monkeypatch):
    """A background-task completion opens the next turn straight through the reader's
    `_open_holder` — it never calls `_ensure_proc`. The prior turn's close let the
    painter exit, so `_open_holder` must revive it, or the card freezes at frame one."""

    async def scenario():
        monkeypatch.setattr(drv, "EDIT_INTERVAL", 0.01)
        c = client(tmp_path)
        t = window(c, tmp_path)
        t.heartbeat_task = None
        t._open_holder()
        assert t.heartbeat_task is not None and not t.heartbeat_task.done()
        await asyncio.sleep(0.05)
        await drain(c, t)
        assert any(mid == t.turn.card_id for mid, _ in c.bot.edits)
        await t._stop_heartbeat()

    asyncio.run(scenario())


def test_open_holder_keeps_a_live_heartbeat_singleton(tmp_path):
    async def scenario():
        c = client(tmp_path)
        t = window(c, tmp_path)
        live = asyncio.create_task(asyncio.sleep(3600))
        t.heartbeat_task = live
        t._open_holder()
        assert t.heartbeat_task is live  # same task, not duplicated
        live.cancel()

    asyncio.run(scenario())


def test_the_heartbeat_exits_once_the_turn_closes(tmp_path, monkeypatch):
    async def scenario():
        monkeypatch.setattr(drv, "EDIT_INTERVAL", 0.01)
        c = client(tmp_path)
        t = window(c, tmp_path)
        t._open_holder()
        task = t.heartbeat_task
        await t._finalize_turn("done")
        await asyncio.wait_for(task, 1)  # returns on its own, no cancel needed
        await drain(c, t)

    asyncio.run(scenario())


def test_opening_a_turn_leaves_the_background_updater_running(tmp_path):
    """The panel's updater is independent of the turn (it no longer hands the panel to
    the heartbeat at turn open)."""

    async def scenario():
        c = client(tmp_path)
        t = window(c, tmp_path)
        updater = asyncio.create_task(asyncio.sleep(3600))
        t.background_updater_task = updater
        opened = t._open_holder()
        if asyncio.iscoroutine(opened):  # holds on an implementation that awaits here
            await opened
        await asyncio.sleep(0)
        assert t.background_updater_task is updater and not updater.done()
        updater.cancel()
        await t._stop_heartbeat()

    asyncio.run(scenario())
