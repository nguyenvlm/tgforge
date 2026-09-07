"""A settled answer whose MarkdownV2 render already fills MAX_MSG still gets a live
background panel appended every updater tick. The compose must keep the answer's end
and the panel within the limit — the transport used to hard-cut the tail (the answer's
final words) when body + panel crossed MAX_MSG."""

from __future__ import annotations

import asyncio

from tgforge.base.ui import MAX_MSG
from tgforge.plugins.claude import ClaudeTopic
from tgforge.plugins.claude import driver as drv
from tgforge.testing import TestClient


def test_background_updater_keeps_answer_tail_under_panel(tmp_path, monkeypatch):
    async def scenario():
        real_sleep = asyncio.sleep
        monkeypatch.setattr(asyncio, "sleep", lambda s: real_sleep(0))
        monkeypatch.setattr(drv, "UPDATER_INTERVAL", 0.01)
        c = TestClient(home=str(tmp_path))
        t = c.core._instantiate(ClaudeTopic, 555, "work")
        t.last_final_id = 88
        # an md render that already exceeds MAX_MSG on its own; appending the panel md
        # keeps it over the limit. The distinctive tail sits at the very end.
        md = "x" * (MAX_MSG + 200) + " CONCLUSIONKEPT"
        plain = "the reply ends with CONCLUSIONKEPT"
        t.last_final_body = (md, plain)
        t.last_final_markup = None
        now = asyncio.get_event_loop().time()
        t.background_tasks = {"j": {"done": None, "start": now, "label": "job", "path": "/no/such"}}

        task = asyncio.create_task(t._background_updater())
        await real_sleep(0.02)  # let it paint at least one tick
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        edits = [txt for mid, txt in c.bot.edits if mid == 88]
        assert edits, "the updater painted the final card at least once"
        last = edits[-1]
        assert len(last) <= MAX_MSG
        assert "CONCLUSIONKEPT" in last  # the answer's tail survived the panel append
        assert "background · 1 job" in last  # the panel is still there

    asyncio.run(scenario())
