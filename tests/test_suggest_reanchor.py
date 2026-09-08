"""A new prompt strands the prior turn's suggestion buttons above it (Telegram can't
move a message). The re-anchor is bound to `submit` — the boundary every prompt path
shares — and deletes + resends the button-carrying message so the prompt reads as
acknowledged. A tapped suggestion clears the anchor so the follow-up never resurrects it."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from tgforge.plugins.claude import ClaudeTopic
from tgforge.testing import TestClient


def _ready_topic(c, tmp_path, monkeypatch):
    t = c.core._instantiate(ClaudeTopic, 555, "work")
    t.holder_id = 42
    t.background_tasks = {}
    t.turn_start = asyncio.get_event_loop().time()

    async def _noop(*a, **k):
        return None

    monkeypatch.setattr(t, "_sync_title", _noop)
    monkeypatch.setattr(t, "_jsonl", lambda: tmp_path / "absent.jsonl")
    monkeypatch.setattr(t, "_save", lambda: None)
    return t


def test_finalize_records_the_suggestion_anchor(tmp_path, monkeypatch):
    async def scenario():
        c = TestClient(home=str(tmp_path))
        t = _ready_topic(c, tmp_path, monkeypatch)
        await t._finalize_turn("here you go\n\n[[suggest]] Yes | No")
        assert t.suggest_anchor is not None
        assert t.suggest_anchor["id"] == 42  # the button-carrying message is tracked
        assert t.suggest_anchor["kb"] is not None

    asyncio.run(scenario())


def test_finalize_without_options_clears_the_anchor(tmp_path, monkeypatch):
    async def scenario():
        c = TestClient(home=str(tmp_path))
        t = _ready_topic(c, tmp_path, monkeypatch)
        t.suggest_anchor = {"id": 7, "plain": "stale", "kb": ["x"]}
        await t._finalize_turn("no buttons this time")
        assert t.suggest_anchor is None  # a button-less turn leaves nothing to re-anchor

    asyncio.run(scenario())


def test_reanchor_deletes_and_resends_below(tmp_path, monkeypatch):
    async def scenario():
        c = TestClient(home=str(tmp_path))
        t = _ready_topic(c, tmp_path, monkeypatch)
        t.suggest_anchor = {"id": 42, "plain": "the answer", "kb": ["kb"]}
        before = len(c.bot.sent)
        await t._reanchor_suggestions()
        assert 42 in c.bot.deleted  # the stranded message is deleted, not edited in place
        assert len(c.bot.sent) > before  # and resent below the new prompt
        assert any("the answer" in txt for _, txt in c.bot.sent)
        assert t.suggest_anchor is not None and t.suggest_anchor["id"] != 42  # re-anchored
        assert t.suggest_anchor["id"] in c.bot.markup_cleared  # buttons re-attached to the resend

    asyncio.run(scenario())


def test_reanchor_keeps_the_background_card_id_in_sync(tmp_path, monkeypatch):
    async def scenario():
        c = TestClient(home=str(tmp_path))
        t = _ready_topic(c, tmp_path, monkeypatch)
        t.suggest_anchor = {"id": 42, "plain": "x", "kb": ["kb"]}
        t.last_final_id = 42  # the same message is a live background-updater card
        await t._reanchor_suggestions()
        assert (
            t.last_final_id == t.suggest_anchor["id"]
        )  # updater follows the resend, not a dead id

    asyncio.run(scenario())


def test_reanchor_is_a_noop_without_an_anchor(tmp_path, monkeypatch):
    async def scenario():
        c = TestClient(home=str(tmp_path))
        t = _ready_topic(c, tmp_path, monkeypatch)
        t.suggest_anchor = None
        await t._reanchor_suggestions()
        assert c.bot.deleted == []  # nothing to move → no message churn

    asyncio.run(scenario())


def test_submit_reanchors_at_the_boundary(tmp_path, monkeypatch):
    async def scenario():
        c = TestClient(home=str(tmp_path))
        t = _ready_topic(c, tmp_path, monkeypatch)
        t.busy = False
        t.suggest_anchor = {"id": 42, "plain": "prior", "kb": ["kb"]}

        async def _noop(*a, **k):
            return None

        monkeypatch.setattr(t, "_ensure_proc", _noop)
        monkeypatch.setattr(t, "_open_holder", _noop)
        monkeypatch.setattr(t, "_write_prompt", _noop)
        monkeypatch.setattr(t, "_env_tag", lambda: "")
        monkeypatch.setattr(t, "_app_brief", lambda: "")
        await t.submit("a new prompt")
        assert 42 in c.bot.deleted  # any prompt path re-anchors the prior buttons

    asyncio.run(scenario())


def test_tapped_suggestion_clears_the_anchor(tmp_path, monkeypatch):
    async def scenario():
        c = TestClient(home=str(tmp_path))
        t = _ready_topic(c, tmp_path, monkeypatch)
        t.suggest_anchor = {"id": 42, "plain": "x", "kb": ["kb"]}
        t._suggested["tok"] = {"options": ["Yes", "No"]}

        async def _noop(*a, **k):
            return None

        monkeypatch.setattr(t._core, "route_as_user", _noop)
        ctx = SimpleNamespace(message=SimpleNamespace(message_id=42))
        await t._on_suggestion(ctx, "tok:0")
        assert t.suggest_anchor is None  # a consumed keyboard is not resurrected on the next turn

    asyncio.run(scenario())


def test_reanchor_survives_a_flood_dropped_resend(tmp_path, monkeypatch):
    async def scenario():
        c = TestClient(home=str(tmp_path))
        t = _ready_topic(c, tmp_path, monkeypatch)
        t.suggest_anchor = {"id": 42, "plain": "x", "kb": ["kb"]}

        async def dropped(*a, **k):
            return None  # the resend is flood-dropped

        monkeypatch.setattr(t, "send_rich", dropped)
        await t._reanchor_suggestions()
        assert 42 in c.bot.deleted
        assert t.suggest_anchor is None  # dropped resend clears the anchor, no dangling old id

    asyncio.run(scenario())
