"""The turn's answer is essential: when a settle send is flood-dropped, the reply
used to vanish silently, leaving only the events-summary holder. _finalize_turn now
retries the send across flood windows so the reply still lands."""

from __future__ import annotations

import asyncio

from tgforge.base.ui import MAX_MSG
from tgforge.plugins.claude import ClaudeTopic
from tgforge.testing import TestClient


def test_finalize_delivers_answer_despite_a_flood_drop(tmp_path, monkeypatch):
    async def scenario():
        real_sleep = asyncio.sleep
        monkeypatch.setattr(asyncio, "sleep", lambda s: real_sleep(0))
        c = TestClient(home=str(tmp_path))
        t = c.core._instantiate(ClaudeTopic, 555, "work")
        t.holder_id = 42
        t.background_tasks = {}
        t.turn_start = asyncio.get_event_loop().time()
        t.turn.timeline.append(("tool", "🔧 Bash: do a thing"))  # makes the events block truthy

        async def _noop():
            return None

        monkeypatch.setattr(t, "_sync_title", _noop)
        monkeypatch.setattr(t, "_jsonl", lambda: tmp_path / "absent.jsonl")
        monkeypatch.setattr(t, "_save", lambda: None)

        calls = {"n": 0}
        real_send_rich = t.send_rich

        async def flaky(text, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                return None  # first chunk dropped under flood-wait
            return await real_send_rich(text, **kw)

        monkeypatch.setattr(t, "send_rich", flaky)

        # long enough to exceed MAX_MSG → the events + chunked-answer branch
        answer = "A" * (MAX_MSG + 100) + " DELIVEREDTAIL"
        await t._finalize_turn(answer)

        assert calls["n"] >= 2  # the dropped send was retried, not abandoned
        sent = " ".join(txt for _, txt in c.bot.sent)
        assert "DELIVEREDTAIL" in sent  # the reply reached a bubble instead of vanishing

    asyncio.run(scenario())


def _ready_topic(c, tmp_path, monkeypatch):
    """A finalize-ready topic with the file-IO side effects stubbed."""
    t = c.core._instantiate(ClaudeTopic, 555, "work")
    t.holder_id = 42
    t.background_tasks = {}
    t.turn_start = asyncio.get_event_loop().time()

    async def _noop():
        return None

    monkeypatch.setattr(t, "_sync_title", _noop)
    monkeypatch.setattr(t, "_jsonl", lambda: tmp_path / "absent.jsonl")
    monkeypatch.setattr(t, "_save", lambda: None)
    return t


def test_send_answer_gives_up_and_logs_loudly(tmp_path, monkeypatch, caplog):
    import logging

    async def scenario():
        real_sleep = asyncio.sleep
        monkeypatch.setattr(asyncio, "sleep", lambda s: real_sleep(0))  # no real waits
        c = TestClient(home=str(tmp_path))
        t = _ready_topic(c, tmp_path, monkeypatch)

        async def always_none(*a, **k):
            return None  # every attempt is flood-dropped

        monkeypatch.setattr(t, "send_rich", always_none)
        with caplog.at_level(logging.ERROR, logger="tgforge"):
            out = await t._send_answer("the reply")
        assert out is None
        assert any("dropped after" in r.message for r in caplog.records)  # loud, not silent

    asyncio.run(scenario())


def test_finalize_branch1_edit_fail_still_sends_the_reply(tmp_path, monkeypatch):
    async def scenario():
        real_sleep = asyncio.sleep
        monkeypatch.setattr(asyncio, "sleep", lambda s: real_sleep(0))
        c = TestClient(home=str(tmp_path))
        t = _ready_topic(c, tmp_path, monkeypatch)  # empty timeline → no events, short answer fits

        async def edit_fail(*a, **k):
            return False  # the holder edit is flood-dropped

        monkeypatch.setattr(t, "edit_md", edit_fail)
        await t._finalize_turn("SHORTREPLYTOKEN")
        sent = " ".join(txt for _, txt in c.bot.sent)
        assert "SHORTREPLYTOKEN" in sent  # fell back to a guaranteed send, not lost

    asyncio.run(scenario())


def test_finalize_branch3_edit_rich_fail_still_sends_first_chunk(tmp_path, monkeypatch):
    async def scenario():
        real_sleep = asyncio.sleep
        monkeypatch.setattr(asyncio, "sleep", lambda s: real_sleep(0))
        c = TestClient(home=str(tmp_path))
        t = _ready_topic(c, tmp_path, monkeypatch)  # empty timeline → no events → the else branch

        async def edit_rich_fail(*a, **k):
            return False  # the first-chunk holder edit is flood-dropped

        monkeypatch.setattr(t, "edit_rich", edit_rich_fail)
        answer = "STARTTOKEN " + "B" * (MAX_MSG) + " ENDTOKEN"  # two chunks
        await t._finalize_turn(answer)
        sent = " ".join(txt for _, txt in c.bot.sent)
        assert "STARTTOKEN" in sent  # chunk 0 fell back to a send instead of vanishing
        assert "ENDTOKEN" in sent  # chunk 1 delivered too

    asyncio.run(scenario())
