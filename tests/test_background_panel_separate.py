"""The background-jobs panel is a SEPARATE message from the turn's final/main card, so
its size can never trim the main text. It lists running jobs plus each finished job's
outcome for a short linger, and is retired once nothing is left to show. See the
`background · N jobs` defect (the panel rode the final message and cut the main text).
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

from tgforge.base.ui import MAX_MSG
from tgforge.plugins.claude import ClaudeTopic, background
from tgforge.testing import TestClient


def _finalize_topic(c, tmp_path, monkeypatch):
    t = c.core._instantiate(ClaudeTopic, 555, "work")
    t.holder_id = 42
    t.turn_start = asyncio.get_event_loop().time()

    async def _noop():
        return None

    monkeypatch.setattr(t, "_sync_title", _noop)
    monkeypatch.setattr(t, "_jsonl", lambda: tmp_path / "absent.jsonl")
    monkeypatch.setattr(t, "_save", lambda: None)
    return t


def _session(tmp_path, finished_ago):
    out = tmp_path / "bid.output"
    out.write_text("still going\n")
    now = time.monotonic()
    return SimpleNamespace(
        background_tasks={
            "run": {"path": str(out), "label": "live-job", "start": now - 60, "done": None},
            "fin": {
                "path": str(out),
                "label": "old-job",
                "start": now - 120,
                "done": "✗",
                "done_at": now - finished_ago,
            },
        },
        background_labels={},
        background_probe_at=0.0,
    )


def test_finished_job_shows_its_outcome_while_it_lingers(tmp_path):
    _md, plain = background.panel(_session(tmp_path, finished_ago=1.0), 0)
    assert "live-job" in plain
    old_row = next(row for row in plain.splitlines() if "old-job" in row)
    assert old_row.startswith("✗") and old_row.endswith("failed")
    assert "background · 1 job · 1 finished" in plain


def test_finished_job_drops_off_after_the_linger(tmp_path):
    s = _session(tmp_path, finished_ago=background.FINISHED_LINGER + 1)
    _md, plain = background.panel(s, 0)
    assert "old-job" not in plain and "✗" not in plain
    assert "background · 1 job" in plain and "finished" not in plain
    background.prune_finished(s, time.monotonic())
    assert list(s.background_tasks) == ["run"]


def test_panel_is_a_separate_message_final_card_has_no_panel(tmp_path, monkeypatch):
    async def scenario():
        real_sleep = asyncio.sleep
        monkeypatch.setattr(asyncio, "sleep", lambda s: real_sleep(0))
        c = TestClient(home=str(tmp_path))
        t = _finalize_topic(c, tmp_path, monkeypatch)
        now = asyncio.get_event_loop().time()
        t.background_tasks = {
            "j": {"done": None, "start": now, "label": "live-job", "path": "/no/such"}
        }

        await t._finalize_turn("The answer. REPLYTOKEN")
        if t.background_updater_task is not None:
            t.background_updater_task.cancel()

        # the final card (the holder, id 42) carries the reply and NO panel section
        final = " ".join(txt for mid, txt in c.bot.edits if mid == 42)
        assert "REPLYTOKEN" in final
        assert "background ·" not in final
        # the panel rode its own, separate message
        assert t.background_panel_id is not None and t.background_panel_id != 42
        panel_msgs = [txt for _tid, txt in c.bot.sent if "background ·" in txt]
        assert panel_msgs and "live-job" in panel_msgs[-1]

    asyncio.run(scenario())


def test_finished_job_lingers_after_the_turn_then_the_updater_keeps_the_panel(
    tmp_path, monkeypatch
):
    async def scenario():
        real_sleep = asyncio.sleep
        monkeypatch.setattr(asyncio, "sleep", lambda s: real_sleep(0))
        c = TestClient(home=str(tmp_path))
        t = _finalize_topic(c, tmp_path, monkeypatch)
        now = time.monotonic()
        t.background_tasks = {
            "j": {"done": "✓", "start": now - 5, "done_at": now, "label": "new-job", "path": "/x"}
        }

        await t._finalize_turn("Done. REPLYTOKEN")

        assert "j" in t.background_tasks  # still lingering
        panel_msgs = [txt for _tid, txt in c.bot.sent if "background ·" in txt]
        assert panel_msgs and "✓ new-job" in panel_msgs[-1]
        assert t.background_updater_task is not None  # it will drop the row later
        t.background_updater_task.cancel()

    asyncio.run(scenario())


def test_finished_job_pruned_and_panel_retired_when_none_run(tmp_path, monkeypatch):
    async def scenario():
        real_sleep = asyncio.sleep
        monkeypatch.setattr(asyncio, "sleep", lambda s: real_sleep(0))
        c = TestClient(home=str(tmp_path))
        t = _finalize_topic(c, tmp_path, monkeypatch)
        t.background_panel_id = 88  # a panel message left from when the job was running
        done_at = time.monotonic() - background.FINISHED_LINGER - 1  # lingered out
        t.background_tasks = {
            "j": {"done": "✓", "start": 0.0, "done_at": done_at, "label": "old-job", "path": "/x"}
        }

        await t._finalize_turn("Done. REPLYTOKEN")

        assert t.background_tasks == {}  # the finished job was pruned, not retained forever
        assert 88 in c.bot.deleted  # the panel message was retired
        assert t.background_panel_id is None
        assert t.background_updater_task is None  # nothing running → no between-turn updater

    asyncio.run(scenario())


def test_long_main_text_is_never_trimmed_by_panel_size(tmp_path, monkeypatch):
    """The live frame's main text stays whole no matter how large the panel — the panel is
    its own message. Before the fix the heartbeat reserved room off MAX_MSG for the panel,
    so a huge panel head-trimmed the main text (the user saw a reply start mid-sentence)."""

    async def scenario():
        real_sleep = asyncio.sleep
        monkeypatch.setattr(asyncio, "sleep", lambda s: real_sleep(0))
        import tgforge.plugins.claude.driver as drv

        monkeypatch.setattr(drv, "EDIT_INTERVAL", 0.001)
        c = TestClient(home=str(tmp_path))
        t = c.core._instantiate(ClaudeTopic, 555, "work")
        t.busy = True
        t.holder_id = 42
        t.turn_start = asyncio.get_event_loop().time()
        # a live main text that fits MAX_MSG on its own, with distinctive ends
        t.turn.preview_parts = ["MAINSTART " + "y " * ((MAX_MSG - 300) // 2) + " MAINEND"]
        # a huge panel: many running jobs, far past one Telegram message on their own
        now = asyncio.get_event_loop().time()
        t.background_tasks = {
            f"j{i}": {"done": None, "start": now, "label": f"job-{i}", "path": "/no/such"}
            for i in range(400)
        }

        t.heartbeat_task = asyncio.create_task(t._heartbeat())
        await real_sleep(0.02)
        t.heartbeat_task.cancel()
        try:
            await t.heartbeat_task
        except asyncio.CancelledError:
            pass

        holder_edits = [txt for mid, txt in c.bot.edits if mid == 42]
        assert holder_edits, "the heartbeat painted the holder"
        assert all(len(txt) <= MAX_MSG for txt in holder_edits)
        # both ends of the main text survived — panel size never cut it
        assert "MAINSTART" in holder_edits[-1] and "MAINEND" in holder_edits[-1]
        assert "background ·" not in holder_edits[-1]  # panel is not in the main card
        # the panel rode its own message, itself trimmed to fit
        panel_msgs = [txt for _tid, txt in c.bot.sent if "background ·" in txt]
        assert panel_msgs and all(len(txt) <= MAX_MSG for txt in panel_msgs)

    asyncio.run(scenario())


def test_updater_retires_the_panel_once_the_last_row_lingers_out(tmp_path, monkeypatch):
    async def scenario():
        import tgforge.plugins.claude.driver as drv

        monkeypatch.setattr(drv, "UPDATER_INTERVAL", 0.01)
        monkeypatch.setattr(background, "FINISHED_LINGER", 0.2)
        c = TestClient(home=str(tmp_path))
        t = c.core._instantiate(ClaudeTopic, 555, "work")
        now = time.monotonic()
        t.background_tasks = {
            "j": {"done": "✓", "start": now - 5, "done_at": now, "label": "new-job", "path": "/x"}
        }
        t._ensure_background_updater()
        await asyncio.wait_for(t.background_updater_task, 2)  # ends by itself

        panel_msgs = [txt for _tid, txt in c.bot.sent if "background ·" in txt]
        assert panel_msgs and "✓ new-job" in panel_msgs[0]  # shown while it lingered
        assert t.background_panel_id is None  # then retired
        assert t.background_tasks == {}  # and forgotten

    asyncio.run(scenario())
