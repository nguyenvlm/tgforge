"""The path-input pair: `ask_text` carries a ✖ Cancel (typed reply → the text, tap →
None), and `pick_dir` browses folders in one message (descend, .., Use, Cancel)."""

from __future__ import annotations

import asyncio

from tgforge.base.kernel import Topic
from tgforge.testing import TestClient

# ── ask_text ───────────────────────────────────────────────────────


def test_ask_text_typed_reply_returns_the_text(tmp_path):
    async def scenario():
        c = TestClient(home=str(tmp_path))
        task = asyncio.create_task(c.core.ask_text(100, None, "Path?"))
        await c.pump()
        assert "✖ Cancel" in c.buttons  # a prompt is escapable
        await c.send("some/typed/path")  # feeds the pending prompt
        assert await asyncio.wait_for(task, 2) == "some/typed/path"

    asyncio.run(scenario())


def test_ask_text_cancel_tap_returns_none(tmp_path):
    async def scenario():
        c = TestClient(home=str(tmp_path))
        task = asyncio.create_task(c.core.ask_text(100, None, "Path?"))
        await c.pump()
        await c.tap("✖ Cancel")
        assert await asyncio.wait_for(task, 2) is None

    asyncio.run(scenario())


# ── pick_dir ───────────────────────────────────────────────────────


def _topic(client: TestClient) -> Topic:
    return Topic(client.core, None, "picker", None)


def test_pick_dir_descend_then_use_returns_that_folder(tmp_path):
    (tmp_path / "alpha" / "child").mkdir(parents=True)
    (tmp_path / "beta").mkdir()

    async def scenario():
        c = TestClient(home=str(tmp_path))
        task = asyncio.create_task(_topic(c).pick_dir(start=str(tmp_path)))
        await c.pump()
        assert "📁 alpha" in c.buttons and "📁 beta" in c.buttons
        await c.tap("📁 alpha")
        assert "📁 child" in c.buttons  # re-rendered inside alpha
        await c.tap("✓ Use alpha")
        assert await asyncio.wait_for(task, 2) == str(tmp_path / "alpha")

    asyncio.run(scenario())


def test_pick_dir_up_returns_to_parent(tmp_path):
    (tmp_path / "alpha").mkdir()
    (tmp_path / "beta").mkdir()

    async def scenario():
        c = TestClient(home=str(tmp_path))
        task = asyncio.create_task(_topic(c).pick_dir(start=str(tmp_path)))
        await c.pump()
        await c.tap("📁 alpha")
        assert "📁 beta" not in c.buttons  # alpha has no sibling inside it
        await c.tap("⬆ ..")
        assert "📁 beta" in c.buttons  # back at the parent
        await c.tap("✖ Cancel")
        assert await asyncio.wait_for(task, 2) is None

    asyncio.run(scenario())


def test_pick_dir_pages_long_listings(tmp_path):
    for i in range(15):  # > one page (12)
        (tmp_path / f"d{i:02d}").mkdir()

    async def scenario():
        c = TestClient(home=str(tmp_path))
        task = asyncio.create_task(_topic(c).pick_dir(start=str(tmp_path)))
        await c.pump()
        assert "📁 d00" in c.buttons and "📁 d14" not in c.buttons  # first page
        assert "next ›" in c.buttons
        await c.tap("next ›")
        assert "📁 d14" in c.buttons and "📁 d00" not in c.buttons  # second page
        await c.tap("✖ Cancel")
        assert await asyncio.wait_for(task, 2) is None

    asyncio.run(scenario())


def test_pick_dir_cancel_returns_none(tmp_path):
    async def scenario():
        c = TestClient(home=str(tmp_path))
        task = asyncio.create_task(_topic(c).pick_dir(start=str(tmp_path)))
        await c.pump()
        await c.tap("✖ Cancel")
        assert await asyncio.wait_for(task, 2) is None

    asyncio.run(scenario())
