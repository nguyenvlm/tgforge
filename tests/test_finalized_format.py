"""Finalized-turn format guard: the timeline is FOLDED into a collapsed expandable
bubble (ui.expandable → the `**>` / `||` markers), never left as the live expanded
heartbeat frame (status_head + inline lines). See AGENTS.md → Finalized-turn format.

Note: the "settled card reads as the working timeline" report was a downstream symptom
of the turn-boundary race (a phantom holder reopened after finalize); it is resolved by
the _finalize_turn lock/reorder fix, not by a separate finalize-format change. This is
the positive guard that the fold itself keeps working."""

from __future__ import annotations

import asyncio

from tgforge.base.ui import MAX_MSG
from tgforge.plugins.claude import ClaudeTopic
from tgforge.testing import TestClient


def test_finalized_timeline_is_a_folded_expandable_not_the_live_frame(tmp_path, monkeypatch):
    async def scenario():
        real_sleep = asyncio.sleep
        monkeypatch.setattr(asyncio, "sleep", lambda s: real_sleep(0))
        c = TestClient(home=str(tmp_path))
        t = c.core._instantiate(ClaudeTopic, 555, "work")
        t.holder_id = 42
        t.background_tasks = {}
        t.turn_start = asyncio.get_event_loop().time()

        async def _noop():
            return None

        monkeypatch.setattr(t, "_sync_title", _noop)
        monkeypatch.setattr(t, "_jsonl", lambda: tmp_path / "absent.jsonl")
        monkeypatch.setattr(t, "_save", lambda: None)
        t.turn.timeline.append(("text", "First I look at the file."))
        t.turn.timeline.append(("tool", "🔧 Bash: inspect the repo"))

        await t._finalize_turn("A short reply. REPLYTOKEN")

        # the finalized card carries the collapsed-expandable markers (folded), and the
        # reply — not the live status_head frame
        card = " ".join(text for _mid, text in c.bot.edits)
        assert "**>" in card and "||" in card, "timeline was not folded into an expandable"
        assert "REPLYTOKEN" in card, "the reply was not rendered into the finalized card"

    asyncio.run(scenario())


def test_overflow_fold_edit_is_retried_when_dropped(tmp_path, monkeypatch):
    """Hardening: on overflow, the fold-edit that collapses the timeline into the holder
    must be retried across a flood drop (like the reply is), so a dropped edit doesn't
    leave the live working frame as the finalized card."""

    async def scenario():
        real_sleep = asyncio.sleep
        monkeypatch.setattr(asyncio, "sleep", lambda s: real_sleep(0))
        c = TestClient(home=str(tmp_path))
        t = c.core._instantiate(ClaudeTopic, 555, "work")
        t.holder_id = 42
        t.background_tasks = {}
        t.turn_start = asyncio.get_event_loop().time()

        async def _noop():
            return None

        monkeypatch.setattr(t, "_sync_title", _noop)
        monkeypatch.setattr(t, "_jsonl", lambda: tmp_path / "absent.jsonl")
        monkeypatch.setattr(t, "_save", lambda: None)
        t.turn.timeline.append(("text", "First I look at the file."))
        t.turn.timeline.append(("tool", "🔧 Bash: inspect the repo"))

        fold_calls = {"n": 0}
        real_edit_md = t.edit_md

        async def edit_md_spy(mid, md, plain, **kw):
            if "🔧" in md:  # the folded-timeline edit into the holder
                fold_calls["n"] += 1
                if fold_calls["n"] == 1:
                    return False  # dropped under flood on the first try
            return await real_edit_md(mid, md, plain, **kw)

        monkeypatch.setattr(t, "edit_md", edit_md_spy)

        answer = "REPLY_START " + "x " * (MAX_MSG) + " REPLY_END"  # overflows one message
        await t._finalize_turn(answer)

        assert fold_calls["n"] >= 2, "the folded-timeline edit was abandoned after one drop"

    asyncio.run(scenario())
