"""Button-label clamping, the empty model default, and the core menu's option
assembly (built from the registry, not per-plugin knowledge)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from test_core_routing import MockBot

from tgforge.base.config import BotConfig
from tgforge.base.kernel import Plugin, Topic, command, launch, universal
from tgforge.base.ui import BTN_MAX, blabel
from tgforge.plugins.claude.config import DEFAULT_MODELS


async def _tap(core, label):
    """Resolve whichever pending menu prompt currently renders a button whose text
    matches `label` — a simulated Telegram tap."""
    for _ in range(200):
        await asyncio.sleep(0)
        kb = core.bot.last_markup
        if kb is not None:
            for row in kb.inline_keyboard:
                for btn in row:
                    if btn.text == blabel(label):
                        core.bot.last_markup = None
                        cb = SimpleNamespace(
                            data=btn.callback_data,
                            message=None,
                            answer=lambda *a, **k: _noop(),
                        )
                        await core.handle_callback(cb)
                        return
    raise AssertionError(f"button {label!r} never rendered")


async def _noop():
    return None


def test_blabel_leaves_short_text_untouched():
    assert blabel("🤖 Claude") == "🤖 Claude"


def test_blabel_clamps_long_text_keeping_head_and_tail():
    long = "/opt/projects/example-workspace/very/deep/nested/dir"
    out = blabel(long)
    assert len(out) <= BTN_MAX
    assert "…" in out
    assert out.startswith("/opt")
    assert out.endswith("dir")


def test_default_models_ship_empty():
    assert DEFAULT_MODELS == []  # no model ids baked into the library


@launch("/shell", "open a shell")
class _ShellTopic(Topic):
    id = "shell"
    icon = "🖥"
    menu_label = "Shell"

    @command("/kill", "kill", icon="🔪")  # one-off → triggered marker, no Back
    async def kill(self, ctx):
        await self.send("killed")

    @command("/peek", "peek", icon="👀", inline=True)  # inline → renders in place + Back
    async def peek(self, ctx):
        return "peeked-body"


class _Shell(Plugin):
    id = "shell"
    topics = [_ShellTopic]

    @universal("/ping", "ping")  # no icon → fallback glyph in the menu
    async def ping(self, ctx): ...

    @universal("/reboot", "reboot", icon="♻️")  # its own glyph
    async def reboot(self, ctx): ...

    @universal("/secret", "secret", menu="none")  # never in the menu
    async def secret(self, ctx): ...

    @universal("/inwin", "in-window", icon="🪟", menu="window")  # only inside a window
    async def inwin(self, ctx): ...

    @universal("/stat", "stat", icon="📈", inline=True)  # inline text universal
    async def stat(self, ctx):
        return "stat-body"


def _kernel_with_shell():
    from tgforge.base.kernel import Kernel

    cfg = BotConfig(token="x", home="/tmp", owner_id=1, chat_id=100, bot_username="bot")
    return Kernel(MockBot(), cfg, [_Shell()])


def test_settle_prompt_announces_choice_in_place():
    async def scenario():
        core = _kernel_with_shell()
        opts = [("🤖 Claude", "open:/claude"), ("🖥 Shell", "open:/shell")]
        await core._settle_prompt(100, 555, opts, idx=1, announce=lambda label: f"🚀 {label}")
        return core.bot

    bot = asyncio.run(scenario())
    assert (555, "🚀 🖥 Shell") in bot.edits  # message rewritten to the outcome
    assert 555 not in bot.markup_cleared  # announced, not merely stripped


def test_settle_prompt_timeout_collapses_to_note():
    async def scenario():
        core = _kernel_with_shell()
        await core._settle_prompt(100, 7, [("a", "a")], idx=None, announce=lambda label: label)
        return core.bot

    bot = asyncio.run(scenario())
    assert (7, "⏱ timed out") in bot.edits


def test_settle_prompt_without_announce_just_strips_buttons():
    async def scenario():
        core = _kernel_with_shell()
        await core._settle_prompt(100, 9, [("a", "a")], idx=0, announce=None)
        return core.bot

    bot = asyncio.run(scenario())
    assert 9 in bot.markup_cleared
    assert bot.edits == []  # text untouched


def test_desktop_menu_shows_launchers_and_allowed_universals():
    core = _kernel_with_shell()
    opts = core._menu_options(thread_id=None)  # the desktop (General / core)
    values = [v for _label, v in opts]
    label_of = {v: label for label, v in opts}
    assert "open:/shell" in values  # launchers appear on the desktop
    assert label_of["open:/shell"] == "🖥 Shell"  # label from the class
    assert "cmd:/reboot" in values and label_of["cmd:/reboot"] == "♻️ Reboot"
    assert label_of["cmd:/ping"] == "▫️ Ping"  # no icon → fallback glyph, still shown
    assert "cmd:/secret" not in values  # menu="none" is never shown
    assert "cmd:/inwin" not in values  # menu="window" hidden off the desktop
    assert "ccmd:/kill" not in values  # class commands need their window
    assert values[-1] == "cmd:/help"  # Help is the trailing leaf (inline)
    assert all(len(label) <= BTN_MAX for label, _ in opts)


def test_window_menu_shows_class_commands_and_window_universals():
    async def scenario():
        core = _kernel_with_shell()
        inst = await core.open_window(100, _ShellTopic, "work")
        return core, inst.thread_id

    core, tid = asyncio.run(scenario())
    opts = core._menu_options(thread_id=tid)
    values = [v for _label, v in opts]
    label_of = {v: label for label, v in opts}
    assert "ccmd:/kill" in values and label_of["ccmd:/kill"] == "🔪 Kill"  # its own command
    assert "ccmd:/peek" in values  # inline class command still a button
    assert "cmd:/inwin" in values  # menu="window" now allowed
    assert "cmd:/reboot" in values  # menu="any" still allowed
    assert "cmd:/secret" not in values  # menu="none" still hidden
    assert not any(v.startswith("open:") for v in values)  # launchers hidden inside a window


def test_menu_header_names_the_context():
    async def scenario():
        core = _kernel_with_shell()
        inst = await core.open_window(100, _ShellTopic, "work")
        return core, inst.thread_id

    core, tid = asyncio.run(scenario())
    assert core._menu_header(None) == "🧭 Menu"  # the desktop title, never a lone emoji
    assert core._menu_header(tid) == "🖥 Shell"  # a window → its own icon + name


def test_window_title_carries_icon_and_capitalized_name():
    core = _kernel_with_shell()
    assert core._window_title(_ShellTopic, None) == "🖥 Shell"  # default: icon + menu label
    assert core._window_title(_ShellTopic, "myproj") == "🖥 myproj"  # custom name, icon prefixed


def test_open_window_and_rename_keep_the_icon():
    async def scenario():
        core = _kernel_with_shell()
        inst = await core.open_window(100, _ShellTopic, None)
        opened = core._names()[inst.thread_id]
        await core.rename_topic(inst.thread_id, "deploy logs")
        return opened, core._names()[inst.thread_id]

    opened, renamed = asyncio.run(scenario())
    assert opened == "🖥 Shell"  # created with the icon + capitalized label
    assert renamed == "🖥 deploy logs"  # a rename keeps the class icon


def test_menu_entry_falls_back_to_id_without_icon():
    @launch("/plain", "plain")
    class _Plain(Topic):
        id = "plain"

    assert _Plain.menu_entry() == "Plain"  # no icon/label declared → id.title()


def test_is_inline_reads_each_declaration():
    async def scenario():
        core = _kernel_with_shell()
        inst = await core.open_window(100, _ShellTopic, "work")
        return core, inst.thread_id

    core, tid = asyncio.run(scenario())
    assert core._is_inline("cmd:/stat", tid) is True  # inline universal
    assert core._is_inline("cmd:/reboot", tid) is False  # non-inline universal
    assert core._is_inline("ccmd:/peek", tid) is True  # inline class command
    assert core._is_inline("ccmd:/kill", tid) is False  # non-inline class command
    assert core._is_inline("open:/shell", tid) is False  # a launcher is never inline
    assert core._is_inline("cmd:/help", tid) is True  # Help renders inline


def test_aliases_resolve_to_primary():
    core = _kernel_with_shell()
    assert core.registry.resolve_universal("/menu") == "/"  # alias of the opener
    assert core.registry.resolve_universal("/?") == "/help"  # alias of help
    assert core.registry.resolve_universal("/nope") is None


def test_inline_renders_in_place_with_back_then_reopens_menu():
    async def scenario():
        core = _kernel_with_shell()
        inst = await core.open_window(100, _ShellTopic, "work")
        tid = inst.thread_id
        task = asyncio.create_task(core.show_menu(100, tid))
        await _tap(core, "👀 Peek")  # inline → renders in place + Back
        await _tap(core, "⬅ Back")  # Back re-renders the menu on the same message
        await _tap(core, "🔪 Kill")  # a one-off → marks triggered, ends the menu
        await task
        return core.bot

    bot = asyncio.run(scenario())
    # the inline body rendered on the menu message (an edit), never a separate send
    assert any(text == "peeked-body" for _mid, text in bot.edits)
    assert "peeked-body" not in [t for _tid, t in bot.sent]
    # the one-off marked the menu as triggered, and did its own output
    assert any(text == "▸ 🔪 Kill" for _mid, text in bot.edits)
    assert "killed" in [t for _tid, t in bot.sent]


def test_one_off_marks_triggered_no_back():
    async def scenario():
        core = _kernel_with_shell()
        inst = await core.open_window(100, _ShellTopic, "work")
        tid = inst.thread_id
        task = asyncio.create_task(core.show_menu(100, tid))
        await _tap(core, "🔪 Kill")  # one-off → menu ends immediately, no Back offered
        await asyncio.wait_for(task, timeout=1)  # would hang if a Back prompt were waiting
        return core.bot

    bot = asyncio.run(scenario())
    assert any(text == "▸ 🔪 Kill" for _mid, text in bot.edits)  # triggered marker
    assert not any(text == "peeked-body" for _mid, text in bot.edits)  # no inline render
