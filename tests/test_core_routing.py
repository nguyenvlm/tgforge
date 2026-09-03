"""The core: chain-of-responsibility routing, launch-command context behavior,
window lifecycle (open / adopt / release / close), usage counts, and restart
revival — all driven through a mock aiogram bot."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from tgforge.base.config import BotConfig
from tgforge.base.kernel import (
    Kernel,
    Plugin,
    Topic,
    command,
    launch,
    on_message,
    on_unknown,
    universal,
)
from tgforge.testing import MockBot  # re-exported for the other test modules

EVENTS: list[tuple] = []


@launch("/shell", "open a shell")
class ShellTopic(Topic):
    id = "shell"

    async def on_open(self):
        EVENTS.append(("open", self.thread_id))

    async def on_revive(self):
        EVENTS.append(("revive", self.thread_id))

    async def on_close(self):
        EVENTS.append(("close", self.thread_id))

    @command("/c", "ctrl-c")
    async def interrupt(self, ctx):
        EVENTS.append(("cmd", ctx.text))

    @on_message
    async def feed(self, ctx):
        if ctx.text.startswith("/"):
            return False  # let an unknown slash fall to on_unknown
        EVENTS.append(("msg", ctx.text))

    @on_unknown
    async def unknown(self, ctx):
        EVENTS.append(("unknown", ctx.text))


class Shell(Plugin):
    id = "shell"
    topics = [ShellTopic]

    @universal("/ping", "ping", aliases=("/p",))
    async def ping(self, ctx):
        await ctx.send("pong")


def _core(tmp_path):
    cfg = BotConfig(token="x", home=str(tmp_path), owner_id=1, chat_id=100, bot_username="bot")
    core = Kernel(MockBot(), cfg, [Shell()])
    return core


def _msg(text, thread_id):
    return SimpleNamespace(
        text=text,
        caption=None,
        photo=None,
        document=None,
        from_user=SimpleNamespace(id=1),
        chat=SimpleNamespace(id=100),
        message_thread_id=thread_id,
    )


def setup_function():
    EVENTS.clear()


def test_launch_in_general_opens_window(tmp_path):
    async def scenario():
        core = _core(tmp_path)
        await core.handle_message(_msg("@bot /shell", None))
        tid = next(t for t in core.owners)
        assert core.owners[tid] == "shell"
        assert ("open", tid) in EVENTS

    asyncio.run(scenario())


def test_class_command_and_on_message(tmp_path):
    async def scenario():
        core = _core(tmp_path)
        inst = await core.open_window(100, ShellTopic, "work")
        tid = inst.thread_id
        await core.handle_message(_msg("/c", tid))
        await core.handle_message(_msg("hello world", tid))
        assert ("cmd", "/c") in EVENTS
        assert ("msg", "hello world") in EVENTS

    asyncio.run(scenario())


def test_unknown_slash_hits_on_unknown_not_command(tmp_path):
    async def scenario():
        core = _core(tmp_path)
        inst = await core.open_window(100, ShellTopic, "work")
        await core.handle_message(_msg("/nope extra", inst.thread_id))
        assert ("unknown", "/nope extra") in EVENTS

    asyncio.run(scenario())


def test_escape_prefix_bypasses_class_command(tmp_path):
    async def scenario():
        core = _core(tmp_path)
        inst = await core.open_window(100, ShellTopic, "work")
        tid = inst.thread_id
        await core.handle_message(_msg("/c", tid))  # class command runs
        await core.handle_message(_msg("//c", tid))  # bypass it → falls to on_unknown
        assert ("cmd", "/c") in EVENTS
        assert ("unknown", "/c") in EVENTS  # extras stripped to one slash

    asyncio.run(scenario())


def test_escape_prefix_bypasses_universal(tmp_path):
    async def scenario():
        core = _core(tmp_path)
        inst = await core.open_window(100, ShellTopic, "work")
        tid = inst.thread_id
        await core.handle_message(_msg("//ping", tid))  # bypass the /ping universal
        assert ("unknown", "/ping") in EVENTS  # reaches the window instead
        assert not any(s == "pong" for _t, s in core.bot.sent)  # universal did not run

    asyncio.run(scenario())


def test_launch_in_other_window_is_ignored_falls_to_on_message(tmp_path):
    async def scenario():
        core = _core(tmp_path)
        inst = await core.open_window(100, ShellTopic, "work")
        # /shell is a launch, but inside a shell window it passes → the catch chain
        # takes it (on_message PASSes the slash, on_unknown records it)
        await core.handle_message(_msg("/shell", inst.thread_id))
        assert ("unknown", "/shell") in EVENTS

    asyncio.run(scenario())


def test_universal_ping_runs_in_window(tmp_path):
    async def scenario():
        core = _core(tmp_path)
        inst = await core.open_window(100, ShellTopic, "work")
        core.bot.sent.clear()
        await core.handle_message(_msg("/ping", inst.thread_id))
        assert any("pong" in t for _tid, t in core.bot.sent)

    asyncio.run(scenario())


def test_universal_alias_routes_to_same_handler(tmp_path):
    async def scenario():
        core = _core(tmp_path)
        inst = await core.open_window(100, ShellTopic, "work")
        core.bot.sent.clear()
        await core.handle_message(_msg("/p", inst.thread_id))  # alias of /ping
        assert any("pong" in t for _tid, t in core.bot.sent)

    asyncio.run(scenario())


def test_usage_counts(tmp_path):
    async def scenario():
        core = _core(tmp_path)
        inst = await core.open_window(100, ShellTopic, "work")
        await core.handle_message(_msg("/c", inst.thread_id))
        await core.handle_message(_msg("/c", inst.thread_id))
        totals = dict(((s, n), c) for s, n, c in core.db.usage_totals())
        assert totals[("shell", "/c")] == 2

    asyncio.run(scenario())


def test_release_then_close(tmp_path):
    async def scenario():
        core = _core(tmp_path)
        inst = await core.open_window(100, ShellTopic, "work")
        tid = inst.thread_id
        await core.release_window(tid)
        assert core.owners[tid] == "core"
        assert ("close", tid) in EVENTS
        await core.close_window(tid)
        assert tid in core.bot.deleted_topics
        assert tid not in core.owners

    asyncio.run(scenario())


def test_adopt_transforms_core_window(tmp_path):
    async def scenario():
        core = _core(tmp_path)
        # open a core window via /new in General
        await core.handle_message(_msg("@bot /new desk", None))
        tid = next(t for t in core.owners)
        assert core.owners[tid] == "core"
        await core.adopt(tid, ShellTopic)
        assert core.owners[tid] == "shell"
        assert ("open", tid) in EVENTS  # new shell instance opened

    asyncio.run(scenario())


def test_restart_revives_windows(tmp_path):
    async def scenario():
        core = _core(tmp_path)
        inst = await core.open_window(100, ShellTopic, "work")
        tid = inst.thread_id
        # a fresh core over the same home reloads ownership and revives
        core2 = _core(tmp_path)
        await core2.startup()
        assert core2.owners[tid] == "shell"
        assert isinstance(core2.instances[tid], ShellTopic)
        assert ("revive", tid) in EVENTS
        core2._reconcile_task.cancel()

    asyncio.run(scenario())


@launch("/quiet", "a button-only window")
class QuietTopic(Topic):
    id = "quiet"  # no on_message / on_unknown — a tap-only window like localfs


class Quiet(Plugin):
    id = "quiet"
    topics = [QuietTopic]


@launch("/raw", "a raw-input window (a shell)")
class RawTopic(Topic):
    id = "raw"
    slash_commands = False  # slashes are input, not commands

    @command("/x", "x")
    async def x(self, ctx):
        EVENTS.append(("cmd", "/x"))

    @on_message
    async def feed(self, ctx):
        EVENTS.append(("raw", ctx.text))


@launch("/picky", "a window that blocks a core universal")
class PickyTopic(Topic):
    id = "picky"

    def command_allowed(self, kind, name):
        return name != "/usage"  # a plugin's full say — even over a core universal


class Raw(Plugin):
    id = "raw"
    topics = [RawTopic]


class Picky(Plugin):
    id = "picky"
    topics = [PickyTopic]


def _kernel(tmp_path, plugins):
    cfg = BotConfig(token="x", home=str(tmp_path), owner_id=1, chat_id=100, bot_username="bot")
    return Kernel(MockBot(), cfg, plugins)


def test_slash_is_raw_input_when_slash_commands_disabled(tmp_path):
    async def scenario():
        core = _kernel(tmp_path, [Raw()])
        inst = await core.open_window(100, RawTopic, "r")
        await core.handle_message(_msg("/x hello", inst.thread_id))
        assert ("raw", "/x hello") in EVENTS  # went to the window as input
        assert ("cmd", "/x") not in EVENTS  # not run as a command

    asyncio.run(scenario())


def test_mentioned_slash_still_runs_command_in_raw_window(tmp_path):
    async def scenario():
        core = _kernel(tmp_path, [Raw()])
        inst = await core.open_window(100, RawTopic, "r")
        await core.handle_message(_msg("@bot /x", inst.thread_id))  # mention forces the command
        assert ("cmd", "/x") in EVENTS

    asyncio.run(scenario())


def test_command_allowed_blocks_core_universal(tmp_path):
    async def scenario():
        core = _kernel(tmp_path, [Picky()])
        inst = await core.open_window(100, PickyTopic, "p")
        opts = core._menu_options(inst.thread_id)
        assert "cmd:/usage" not in [v for _l, v in opts]  # hidden from the menu
        core.bot.sent.clear()
        await core.handle_message(_msg("/usage", inst.thread_id))  # typed, blocked → not run
        assert not any("all-time counts" in t for _t, t in core.bot.sent)

    asyncio.run(scenario())


def test_unhandled_text_in_window_falls_back_to_help_not_menu(tmp_path):
    async def scenario():
        cfg = BotConfig(token="x", home=str(tmp_path), owner_id=1, chat_id=100, bot_username="bot")
        core = Kernel(MockBot(), cfg, [Quiet()])
        inst = await core.open_window(100, QuietTopic, "q")
        core.bot.sent.clear()
        core.bot.last_markup = None
        await core.handle_message(_msg("hello", inst.thread_id))  # plain text, no handler
        assert any("/" in txt for _tid, txt in core.bot.sent)  # the help text was sent
        assert core.bot.last_markup is None  # help is a plain reply, not the menu keyboard

    asyncio.run(scenario())


def test_message_in_unowned_topic_is_ignored(tmp_path):
    """Two bots sharing one forum group both pass the chat_id check; a message in a
    topic this bot doesn't own must be dropped, else the non-owner spams help."""

    async def scenario():
        core = _core(tmp_path)
        inst = await core.open_window(100, ShellTopic, "work")
        core.bot.sent.clear()
        EVENTS.clear()
        # a topic this bot never opened (not in owners) — as if a co-resident bot owns it
        await core.handle_message(_msg("hello", inst.thread_id + 777))
        assert core.bot.sent == []  # stayed silent — no help/unhandled reply
        assert EVENTS == []  # never reached the window's handlers
        await core.handle_message(_msg("hello", inst.thread_id))  # its own topic still routes
        assert ("msg", "hello") in EVENTS

    asyncio.run(scenario())


def test_unhandled_mention_in_general_falls_back_to_help(tmp_path):
    async def scenario():
        core = _core(tmp_path)
        core.bot.sent.clear()
        core.bot.last_markup = None
        await core.handle_message(_msg("@bot hello", None))  # mentioned, unhandled content
        assert core.bot.sent  # help text sent
        assert core.bot.last_markup is None  # not the interactive menu

    asyncio.run(scenario())


def test_bare_mention_in_general_opens_menu(tmp_path):
    async def scenario():
        core = _core(tmp_path)
        core.bot.last_markup = None
        task = asyncio.create_task(core.handle_message(_msg("@bot", None)))  # empty mention
        for _ in range(10):
            await asyncio.sleep(0)
        assert core.bot.last_markup is not None  # a bare @mention summons the menu
        task.cancel()

    asyncio.run(scenario())


def test_startup_pings_online_only_in_general(tmp_path):
    async def scenario():
        core = _core(tmp_path)
        inst = await core.open_window(100, ShellTopic, "work")
        tid = inst.thread_id
        core2 = _core(tmp_path)  # a fresh boot over the same home (revives the window)
        await core2.startup()
        online = [t for t, txt in core2.bot.sent if "online" in txt]
        assert online == [None]  # exactly one ping, in General — not into the window
        assert tid not in online
        core2._reconcile_task.cancel()

    asyncio.run(scenario())


def test_general_ignores_unmentioned(tmp_path):
    async def scenario():
        core = _core(tmp_path)
        before = len(core.bot.sent)
        await core.handle_message(_msg("just chatting", None))
        assert len(core.bot.sent) == before

    asyncio.run(scenario())
