"""The TestClient harness itself — drive a bot with plain calls, read what it
sent, and tap menu buttons, with no real Telegram."""

from __future__ import annotations

import asyncio

from tgforge.base.kernel import Plugin, Topic, command, launch, on_unknown, universal
from tgforge.testing import TestClient


@launch("/demo", "open a demo window")
class DemoTopic(Topic):
    id = "demo"
    icon = "🧪"
    menu_label = "Demo"

    async def on_open(self):
        await self.send("demo ready")

    @command("/hi", "say hi", icon="👋", inline=True)  # inline → renders in place + Back
    async def hi(self, ctx):
        return "hi there"

    @command("/go", "go", icon="🏃")  # one-off → triggered marker, no Back
    async def go(self, ctx):
        await self.send("going")

    @command("/pick", "pick one", icon="🎯")  # opens a submenu via ctx.menu
    async def pick(self, ctx):
        choice = await ctx.menu("Pick a fruit", [("🍎 Apple", "apple"), ("🍌 Banana", "banana")])
        if choice:
            await self.send(f"picked {choice}")

    @on_unknown
    async def skill(self, ctx):  # stands in for the agent — catches leftover slashes
        await self.send(f"skill: {ctx.text}")


class Demo(Plugin):
    id = "demo"
    topics = [DemoTopic]

    @universal("/ping", "ping", icon="🏓")
    async def ping(self, ctx):
        await ctx.send("pong")


def test_mention_opens_window_and_command_replies():
    async def scenario():
        bot = TestClient(Demo())
        await bot.send("@bot /demo")  # General mention opens a window
        tid = bot.window
        assert tid is not None and bot.core.owners[tid] == "demo"
        assert "demo ready" in bot.replies
        await bot.send("/hi", thread_id=tid)  # inline class command, typed → sent
        await bot.settle()
        assert "hi there" in bot.replies

    asyncio.run(scenario())


def test_universal_runs_in_window():
    async def scenario():
        bot = TestClient(Demo())
        await bot.send("@bot /demo")
        await bot.send("/ping", thread_id=bot.window)
        await bot.settle()
        assert "pong" in bot.replies

    asyncio.run(scenario())


def test_menu_tap_back_loop_via_tester():
    async def scenario():
        bot = TestClient(Demo())
        await bot.send("@bot /demo")
        tid = bot.window
        await bot.send("/", thread_id=tid)  # opens the context menu
        assert "👋 Hi" in bot.buttons  # the class command is a button
        await bot.tap("👋 Hi")  # inline → renders in place (an edit) + Back
        assert "hi there" in bot.edits
        await bot.tap("⬅ Back")  # back to the menu
        await bot.tap("🏃 Go")  # one-off → marks triggered, does its own output, ends
        await bot.settle()
        assert "going" in bot.replies
        assert "▸ 🏃 Go" in bot.edits  # the menu message marked triggered

    asyncio.run(scenario())


def test_submenu_nests_with_back_and_pick_returns_to_parent():
    async def scenario():
        bot = TestClient(Demo())
        await bot.send("@bot /demo")
        tid = bot.window
        await bot.send("/", thread_id=tid)  # the context menu (nav root)
        assert "🎯 Pick" in bot.buttons
        await bot.tap("🎯 Pick")  # a command that opens a menu → pushes a submenu
        assert "🍎 Apple" in bot.buttons  # the submenu's options
        assert "⬅ Back" in bot.buttons  # Back appears once the stack is deeper than root
        await bot.tap("🍎 Apple")  # pick → the handler acts, then we return to the parent
        await bot.pump()
        assert "picked apple" in bot.replies
        assert "🎯 Pick" in bot.buttons  # navigated → the context menu is showing again

    asyncio.run(scenario())


def test_submenu_back_returns_without_picking():
    async def scenario():
        bot = TestClient(Demo())
        await bot.send("@bot /demo")
        tid = bot.window
        await bot.send("/", thread_id=tid)
        await bot.tap("🎯 Pick")
        assert "⬅ Back" in bot.buttons
        await bot.tap("⬅ Back")  # pop the submenu → back at the parent, nothing picked
        assert "🎯 Pick" in bot.buttons
        assert not any("picked" in r for r in bot.replies)

    asyncio.run(scenario())


def test_typed_menu_is_top_level_no_back():
    async def scenario():
        bot = TestClient(Demo())
        await bot.send("@bot /demo")
        tid = bot.window
        await bot.send("/pick", thread_id=tid)  # typed → top-level menu, no parent
        assert "🍎 Apple" in bot.buttons
        assert "⬅ Back" not in bot.buttons  # depth 1 (root) → no Back affordance
        await bot.tap("🍌 Banana")
        await bot.settle()
        assert "picked banana" in bot.replies

    asyncio.run(scenario())


def test_escape_prefix_reaches_the_window_via_tester():
    async def scenario():
        bot = TestClient(Demo())
        await bot.send("@bot /demo")
        tid = bot.window
        await bot.send("/ping", thread_id=tid)  # normal → the bot universal
        await bot.send("//ping", thread_id=tid)  # escaped → bypass it → the window
        await bot.settle()
        assert "pong" in bot.replies  # the universal ran once
        assert "skill: /ping" in bot.replies  # the escaped one reached the window
        assert bot.replies.count("pong") == 1  # …and only once

    asyncio.run(scenario())
