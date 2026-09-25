"""The background-jobs panel stays the bottom-most message of its window: whatever
lands below it — a turn's card, a reply, a command's answer, an attachment, the
owner's own message — the next panel paint moves the panel back under it."""

from __future__ import annotations

import asyncio
import contextlib
from types import SimpleNamespace

import pytest
from aiogram.exceptions import TelegramRetryAfter

from tgforge.plugins.claude import ClaudeTopic
from tgforge.testing import TestClient

THREAD = 555


def _topic(c, tmp_path, monkeypatch):
    t = c.core._instantiate(ClaudeTopic, THREAD, "work")
    t.turn_start = asyncio.get_event_loop().time()
    t.background_tasks = {
        "j": {"done": None, "start": t.turn_start, "label": "job", "path": "/no/such"}
    }

    async def _noop():
        return None

    monkeypatch.setattr(t, "_sync_title", _noop)
    monkeypatch.setattr(t, "_jsonl", lambda: tmp_path / "absent.jsonl")
    monkeypatch.setattr(t, "_save", lambda: None)
    return t


def _bottom(c) -> int:
    """The bottom-most live message of the window."""
    return max(mid for mid, tid, _ in c.bot.created if tid == THREAD and mid not in c.bot.deleted)


async def _card_opened(c, t):
    t.busy = True
    t.holder_id = await t.send("⠋ working…")


async def _command_reply(c, t):
    t.proc = None
    await t.cancel(SimpleNamespace(args=""))


async def _attachment(c, t):
    await t.send_photo("image/png", "iVBORw0KGgo=")


async def _owner_message(c, t):
    await c.core.handle_message(c._msg(None, THREAD))  # a sticker: routed nowhere


async def _long_reply(c, t):
    await t.send("reply " * 1500)  # several chunks


@pytest.mark.parametrize(
    "lands_below",
    [_card_opened, _command_reply, _attachment, _owner_message, _long_reply],
    ids=["turn card", "command reply", "attachment", "owner message", "chunked reply"],
)
def test_next_paint_moves_the_panel_back_to_the_bottom(tmp_path, monkeypatch, lands_below):
    async def scenario():
        c = TestClient(home=str(tmp_path))
        t = _topic(c, tmp_path, monkeypatch)
        await t._paint_panel(0)
        first_panel = t.background_panel_id
        assert _bottom(c) == first_panel

        await lands_below(c, t)
        assert _bottom(c) != first_panel  # the panel is buried

        await t._paint_panel(1)  # the next updater / heartbeat tick
        assert _bottom(c) == t.background_panel_id
        assert first_panel in c.bot.deleted  # re-anchored, not duplicated

    asyncio.run(scenario())


def test_finalized_turn_leaves_the_panel_at_the_bottom(tmp_path, monkeypatch):
    """A between-turn panel, then a turn opens below it: after the settle the panel is
    under the finalized reply, not stranded above the turn."""

    async def scenario():
        real_sleep = asyncio.sleep
        monkeypatch.setattr(asyncio, "sleep", lambda s: real_sleep(0))
        c = TestClient(home=str(tmp_path))
        t = _topic(c, tmp_path, monkeypatch)
        await t._paint_panel(0)
        await _card_opened(c, t)
        card = t.holder_id

        await t._finalize_turn("The answer.")
        if t.background_updater_task is not None:
            t.background_updater_task.cancel()

        assert _bottom(c) == t.background_panel_id
        assert t.background_panel_id > card

    asyncio.run(scenario())


def test_an_unburied_panel_is_edited_in_place(tmp_path, monkeypatch):
    async def scenario():
        c = TestClient(home=str(tmp_path))
        t = _topic(c, tmp_path, monkeypatch)
        await t._paint_panel(0)
        first_panel = t.background_panel_id
        await t._paint_panel(1)
        assert t.background_panel_id == first_panel
        assert first_panel not in c.bot.deleted

    asyncio.run(scenario())


def test_panel_sends_are_silent_and_replies_are_not(tmp_path, monkeypatch):
    """Every re-anchor is a new message; only the panel's own sends skip the ping."""

    async def scenario():
        c = TestClient(home=str(tmp_path))
        t = _topic(c, tmp_path, monkeypatch)
        await t._paint_panel(0)  # the first send
        first_panel = t.background_panel_id
        reply_id = await t.send("a reply")
        rich_id = await t.send_rich("a *rich* reply")
        await t._paint_panel(1)  # the re-send below them
        assert t.background_panel_id != first_panel
        assert {first_panel, t.background_panel_id} <= c.bot.silent
        assert reply_id not in c.bot.silent and rich_id not in c.bot.silent

    asyncio.run(scenario())


class _Flood(TelegramRetryAfter):
    def __init__(self, retry_after=25):
        self.retry_after = retry_after


def _live_panels(c) -> list[int]:
    return [
        mid
        for mid, tid, text in c.bot.created
        if tid == THREAD and "background ·" in text and mid not in c.bot.deleted
    ]


def test_a_flooded_re_anchor_keeps_the_old_panel_and_never_stalls(tmp_path, monkeypatch):
    """The re-send is droppable: under a flood-wait the painter returns at once with the
    old panel still up, and the next tick re-anchors."""

    async def scenario():
        c = TestClient(home=str(tmp_path))
        t = _topic(c, tmp_path, monkeypatch)
        await t._paint_panel(0)
        first_panel = t.background_panel_id
        await t.send("a reply")  # buries the panel
        real_send = c.bot.send_message
        flood = {"on": True}

        async def flooded(chat_id, text, **kw):
            if flood["on"] and "background ·" in text:
                raise _Flood()
            return await real_send(chat_id, text, **kw)

        c.bot.send_message = flooded
        landed = await asyncio.wait_for(t._paint_panel(1), 1)  # a 25s sleep would time out
        assert landed is False
        assert t.background_panel_id == first_panel and first_panel not in c.bot.deleted

        flood["on"] = False
        assert await t._paint_panel(2) is True
        assert _live_panels(c) == [t.background_panel_id] and _bottom(c) == t.background_panel_id

    asyncio.run(scenario())


def test_cancelling_the_painter_mid_re_anchor_leaves_one_panel(tmp_path, monkeypatch):
    """A turn opening or settling cancels the painter; a re-send already posted must
    still be tracked and the old panel deleted — never two panels."""

    async def scenario():
        c = TestClient(home=str(tmp_path))
        t = _topic(c, tmp_path, monkeypatch)
        await t._paint_panel(0)
        await t.send("a reply")  # buries the panel
        real_send = c.bot.send_message

        async def slow_panel(chat_id, text, **kw):
            msg = await real_send(chat_id, text, **kw)  # Telegram has posted it...
            if "background ·" in text:
                await asyncio.sleep(0.3)  # ...but the response is still on its way
            return msg

        c.bot.send_message = slow_panel
        painter = asyncio.create_task(t._paint_panel(1))
        await asyncio.sleep(0.05)
        painter.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await painter
        await t._paint_panel(2)  # the next painter's first tick

        assert _live_panels(c) == [t.background_panel_id]
        assert _bottom(c) == t.background_panel_id

    asyncio.run(scenario())


def test_a_topic_rename_re_anchors_the_panel(tmp_path, monkeypatch):
    """Telegram posts a "topic renamed" service message the bot never receives."""

    async def scenario():
        c = TestClient(home=str(tmp_path))
        t = _topic(c, tmp_path, monkeypatch)
        await t._paint_panel(0)
        first_panel = t.background_panel_id
        assert await c.core.rename_topic(THREAD, "a new title")
        await t._paint_panel(1)
        assert t.background_panel_id != first_panel and first_panel in c.bot.deleted

        second_panel = t.background_panel_id
        assert await c.core.rename_topic(THREAD, "a new title")  # unchanged: no API call
        await t._paint_panel(2)
        assert t.background_panel_id == second_panel  # nothing new, edited in place

    asyncio.run(scenario())
