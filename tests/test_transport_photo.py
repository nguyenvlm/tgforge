"""Photo transport: `send_photo_b64` returns the new message id and passes a keyboard
through; `edit_photo_b64` replaces the image in place (True) — both refuse a >10MB
payload (None / False). `Topic.send_photo`/`edit_photo` forward the keyboard."""

from __future__ import annotations

import asyncio
import base64

from tgforge.base import ui
from tgforge.base.kernel import Topic
from tgforge.testing import TestClient

_PNG = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\0" * 32).decode()
_OVERSIZED = base64.b64encode(b"\0" * (10 * 1024 * 1024 + 1)).decode()
_KB = ui.keyboard([[ui.act("Ctrl-C", "term", "int")]])


def test_send_photo_returns_id_and_passes_markup(tmp_path):
    async def scenario():
        c = TestClient(home=str(tmp_path))
        mid = await c.core.send_photo_b64(100, "image/png", _PNG, reply_markup=_KB)
        assert isinstance(mid, int)
        assert c.bot.last_markup is _KB  # keyboard forwarded

    asyncio.run(scenario())


def test_send_photo_oversized_returns_none(tmp_path):
    async def scenario():
        c = TestClient(home=str(tmp_path))
        assert await c.core.send_photo_b64(100, "image/png", _OVERSIZED) is None

    asyncio.run(scenario())


def test_edit_photo_replaces_in_place(tmp_path):
    async def scenario():
        c = TestClient(home=str(tmp_path))
        assert await c.core.edit_photo_b64(100, 7, "image/png", _PNG, reply_markup=_KB) is True
        assert (7, "<photo>") in c.bot.edits  # edited the existing message

    asyncio.run(scenario())


def test_edit_photo_oversized_returns_false(tmp_path):
    async def scenario():
        c = TestClient(home=str(tmp_path))
        assert await c.core.edit_photo_b64(100, 7, "image/png", _OVERSIZED) is False

    asyncio.run(scenario())


def test_topic_send_and_edit_photo_forward_markup(tmp_path):
    async def scenario():
        c = TestClient(home=str(tmp_path))
        t = Topic(c.core, 555, "t", None)
        mid = await t.send_photo("image/png", _PNG, reply_markup=_KB)
        assert isinstance(mid, int) and c.bot.last_markup is _KB
        assert await t.edit_photo(mid, "image/png", _PNG, reply_markup=_KB) is True

    asyncio.run(scenario())
