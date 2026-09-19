"""The background panel is its OWN message, so its size can never trim the main text.
The updater paints only the panel message; even a panel with many running jobs is trimmed
(oldest rows first) to fit MAX_MSG on its own, and the settled final card is never touched."""

from __future__ import annotations

import asyncio

from tgforge.base.ui import MAX_MSG
from tgforge.plugins.claude import ClaudeTopic
from tgforge.plugins.claude import driver as drv
from tgforge.testing import TestClient


def test_background_updater_paints_only_its_own_message(tmp_path, monkeypatch):
    async def scenario():
        real_sleep = asyncio.sleep
        monkeypatch.setattr(asyncio, "sleep", lambda s: real_sleep(0))
        monkeypatch.setattr(drv, "UPDATER_INTERVAL", 0.01)
        c = TestClient(home=str(tmp_path))
        t = c.core._instantiate(ClaudeTopic, 555, "work")
        t.background_panel_id = 88  # a live panel message from a between-turn job
        # many running jobs — the panel body alone would exceed MAX_MSG
        now = asyncio.get_event_loop().time()
        t.background_tasks = {
            f"j{i}": {"done": None, "start": now, "label": f"job-{i}", "path": "/no/such"}
            for i in range(400)
        }

        task = asyncio.create_task(t._background_updater())
        await real_sleep(0.02)  # let it paint at least one tick
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        panel_edits = [txt for mid, txt in c.bot.edits if mid == 88]
        assert panel_edits, "the updater painted the panel message at least once"
        assert all(len(txt) <= MAX_MSG for txt in panel_edits)  # panel trimmed to fit itself
        assert "older job(s) hidden" in panel_edits[-1]  # oldest rows dropped, not the message
        # no other message was ever edited — the panel never rides a main card
        assert {mid for mid, _ in c.bot.edits} == {88}

    asyncio.run(scenario())
