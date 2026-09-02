"""ask_buttons appends a Cancel by default (tap or timeout → None), overridable
with cancel=False (omit) or a string (relabel); a real pick still returns its value."""

from __future__ import annotations

import asyncio

from tgforge.testing import TestClient


def test_cancel_appended_by_default_returns_none(tmp_path):
    async def scenario():
        c = TestClient(home=str(tmp_path))
        task = asyncio.create_task(c.core.ask_buttons(100, None, "Pick?", [("A", "a"), ("B", "b")]))
        await c.pump()
        assert "Cancel" in c.buttons  # auto-appended
        await c.tap("Cancel")
        value, _ = await asyncio.wait_for(task, 2)
        assert value is None

    asyncio.run(scenario())


def test_real_pick_still_returns_its_value(tmp_path):
    async def scenario():
        c = TestClient(home=str(tmp_path))
        task = asyncio.create_task(c.core.ask_buttons(100, None, "Pick?", [("A", "a"), ("B", "b")]))
        await c.pump()
        await c.tap("B")
        value, _ = await asyncio.wait_for(task, 2)
        assert value == "b"

    asyncio.run(scenario())


def test_cancel_false_omits_the_option(tmp_path):
    async def scenario():
        c = TestClient(home=str(tmp_path))
        task = asyncio.create_task(
            c.core.ask_buttons(100, None, "Pick?", [("A", "a")], cancel=False)
        )
        await c.pump()
        assert "Cancel" not in c.buttons
        await c.tap("A")
        value, _ = await asyncio.wait_for(task, 2)
        assert value == "a"

    asyncio.run(scenario())


def test_cancel_string_relabels(tmp_path):
    async def scenario():
        c = TestClient(home=str(tmp_path))
        task = asyncio.create_task(
            c.core.ask_buttons(100, None, "Pick?", [("A", "a")], cancel="Dismiss")
        )
        await c.pump()
        assert "Dismiss" in c.buttons and "Cancel" not in c.buttons
        await c.tap("Dismiss")
        value, _ = await asyncio.wait_for(task, 2)
        assert value is None

    asyncio.run(scenario())


def test_cancel_tap_announces_its_label_not_timeout(tmp_path):
    async def scenario():
        c = TestClient(home=str(tmp_path))
        task = asyncio.create_task(
            c.core.ask_buttons(
                100, None, "Pick?", [("A", "a")], announce=lambda label: f"➜ {label}"
            )
        )
        await c.pump()
        await c.tap("Cancel")
        await asyncio.wait_for(task, 2)
        assert "➜ Cancel" in c.edits  # a cancel tap is not a timeout note

    asyncio.run(scenario())
