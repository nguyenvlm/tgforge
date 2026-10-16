"""A graceful restart stays quiet: the claude driver skips the "exited mid-turn"
warning when the core is shutting down (the session is re-adopted, not lost)."""

from __future__ import annotations

import asyncio

from tgforge.plugins.claude import Claude, ClaudeTopic
from tgforge.testing import TestClient


def _topic(client, tid=555):
    t = client.core._instantiate(ClaudeTopic, tid, "Claude")
    client.core.owners[tid] = "claude"
    t.turn_open = True  # a turn is in flight
    t.turn.card_id = 42
    return t


def test_warns_when_process_dies_mid_turn(tmp_path):
    async def scenario():
        c = TestClient(Claude(), home=str(tmp_path))
        t = _topic(c)
        t._warn_interrupted()
        await c.core.flush_frames()
        assert any("exited mid-turn" in text for _mid, text in c.bot.edits)

    asyncio.run(scenario())


def test_silent_on_graceful_shutdown(tmp_path):
    async def scenario():
        c = TestClient(Claude(), home=str(tmp_path))
        c.core.shutting_down = True  # a restart/drain, not a crash
        t = _topic(c)
        t._warn_interrupted()
        await c.core.flush_frames()
        assert not any("exited mid-turn" in text for _mid, text in c.bot.edits)

    asyncio.run(scenario())


def test_shutting_down_defaults_false(tmp_path):
    c = TestClient(Claude(), home=str(tmp_path))
    assert c.core.shutting_down is False


def test_on_shutdown_settles_the_holder(tmp_path):
    async def scenario():
        c = TestClient(Claude(), home=str(tmp_path))
        t = _topic(c)  # card 42
        await t.on_shutdown()
        assert any("interrupted by a restart" in text for _mid, text in c.bot.edits)
        assert t.turn_open is False  # closed, so the reader's finally won't re-warn

    asyncio.run(scenario())


def test_broadcast_shutdown_flags_settles_and_flushes(tmp_path):
    async def scenario():
        c = TestClient(Claude(), home=str(tmp_path))
        t = _topic(c)
        c.core.instances[t.thread_id] = t
        pending = c.core.land_frame(c.core.chat_id, 77, "a final view")
        await c.core.broadcast_shutdown()
        assert c.core.shutting_down is True
        assert t.turn_open is False  # on_shutdown ran
        assert pending.done()  # pending frames were pushed before the exit

    asyncio.run(scenario())


def test_restart_announce_roundtrip(tmp_path):
    from tgforge.base.service import pop_restart_announce

    (tmp_path / ".restart_announce").write_text("268")
    assert pop_restart_announce(tmp_path) == 268
    assert not (tmp_path / ".restart_announce").exists()  # popped (deleted)
    assert pop_restart_announce(tmp_path) is None  # nothing pending
    (tmp_path / ".restart_announce").write_text("not-an-int")
    assert pop_restart_announce(tmp_path) is None  # garbage ignored


def test_detached_restart_records_the_thread(tmp_path, monkeypatch):
    import tgforge.base.service as svc

    monkeypatch.setattr(svc.subprocess, "run", lambda *a, **k: None)
    monkeypatch.setattr(svc, "IS_MACOS", False)
    svc.detached_restart("svc", tmp_path, announce_thread=99)
    assert (tmp_path / ".restart_announce").read_text() == "99"
