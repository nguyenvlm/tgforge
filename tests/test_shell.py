"""The shell plugin on the new base: a real PTY streams output, tracks cwd, a
revived window lands in the saved cwd, and a full-screen TUI is stopped (dropping
allow_universal) with a one-shot notice instead of streaming redraw noise."""

from __future__ import annotations

import asyncio
import os
import shutil
from types import SimpleNamespace

import pytest

from tgforge.base.config import BotConfig
from tgforge.base.kernel import Kernel
from tgforge.plugins.shell import Shell, ShellTopic


class MockBot:
    def __init__(self):
        self._n = 100
        self.sent: list[tuple] = []
        self.edits = 0

    async def get_me(self):
        return SimpleNamespace(username="bot")

    async def create_forum_topic(self, chat_id, name):
        self._n += 1
        return SimpleNamespace(message_thread_id=self._n)

    async def delete_forum_topic(self, chat_id, message_thread_id):
        return True

    async def edit_forum_topic(self, **kw):
        return True

    async def send_message(self, chat_id, text, **kw):
        self._n += 1
        self.sent.append(text)
        return SimpleNamespace(message_id=self._n)

    async def edit_message_text(self, text, **kw):
        self.edits += 1
        return SimpleNamespace(message_id=kw.get("message_id"))

    async def edit_message_reply_markup(self, **kw):
        return SimpleNamespace(message_id=kw.get("message_id"))

    async def send_chat_action(self, **kw):
        return True

    async def delete_message(self, chat_id, message_id):
        return True


def _kernel(tmp_path):
    cfg = BotConfig(token="x", home=str(tmp_path), owner_id=1, chat_id=100, bot_username="bot")
    return Kernel(MockBot(), cfg, [Shell()])


def _msg(text, thread_id):
    return SimpleNamespace(
        text=text,
        caption=None,
        photo=None,
        document=None,
        message_id=999,
        from_user=SimpleNamespace(id=1),
        chat=SimpleNamespace(id=100),
        message_thread_id=thread_id,
    )


async def _until(predicate, timeout=4.0):
    waited = 0.0
    while waited < timeout:
        if predicate():
            return True
        await asyncio.sleep(0.1)
        waited += 0.1
    return predicate()


def test_shell_streams_output(tmp_path):
    async def scenario():
        core = _kernel(tmp_path)
        inst = await core.open_window(100, ShellTopic, "work")
        await core.handle_message(_msg("echo hello123", inst.thread_id))
        assert await _until(lambda: "hello123" in inst.acc)
        await inst._kill()

    asyncio.run(scenario())


def test_cwd_tracked_and_persisted(tmp_path):
    async def scenario():
        core = _kernel(tmp_path)
        inst = await core.open_window(100, ShellTopic, "work")
        await core.handle_message(_msg("cd /usr", inst.thread_id))
        assert await _until(lambda: inst._pid_cwd() == "/usr")
        # the flush loop snapshots the live cwd into the window store
        assert await _until(lambda: inst.saved.get("cwd") == "/usr", timeout=3.0)
        await inst._kill()

    asyncio.run(scenario())


def test_revive_lands_in_saved_cwd(tmp_path):
    async def scenario():
        core = _kernel(tmp_path)
        inst = await core.open_window(100, ShellTopic, "work")
        tid = inst.thread_id
        inst.saved["cwd"] = "/usr"
        await inst._kill()
        # a fresh kernel over the same home revives the window; the next message
        # spawns a fresh shell in the saved cwd
        core2 = _kernel(tmp_path)
        await core2.startup()
        await core2.handle_message(_msg("pwd", tid))
        inst2 = core2.instances[tid]
        assert await _until(lambda: inst2._pid_cwd() == "/usr")
        core2._reconcile_task.cancel()
        await inst2._kill()

    asyncio.run(scenario())


def test_panel_skips_no_op_render(tmp_path):
    async def scenario():
        core = _kernel(tmp_path)
        inst = await core.open_window(100, ShellTopic, "work")
        inst.msg_id = 500  # the panel message already exists
        inst.acc = "hello output"
        core.bot.edits = 0
        await inst._render()
        await inst._render()  # identical content → skipped, so no plain-fallback flip
        assert core.bot.edits == 1
        await inst._kill()

    asyncio.run(scenario())


def test_feed_preserves_leading_whitespace(tmp_path):
    async def scenario():
        core = _kernel(tmp_path)
        inst = await core.open_window(100, ShellTopic, "work")
        out = tmp_path / "ws.txt"
        await core.handle_message(_msg(f"cat > {out}", inst.thread_id))
        assert await _until(out.exists)  # cat opened the redirect before we feed the line
        await core.handle_message(_msg("   spaced", inst.thread_id))  # leading spaces kept raw
        inst._write(b"\x04")  # Ctrl-D closes cat's stdin
        assert await _until(lambda: out.read_text() == "   spaced\n")
        await inst._kill()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "prog,template",
    [
        ("htop", "htop"),
        ("nano", "nano {f}"),
        ("vim", "vim -n {f}"),
        ("less", "less {f}"),
        ("top", "top"),  # no alt-screen — caught via application cursor-keys (?1h)
    ],
)
def test_real_tui_is_detected_and_shell_handed_back(tmp_path, prog, template):
    """A real full-screen program is detected, stopped, and the shell handed back —
    the actual binaries, skipped where absent. None self-exit, so the terminal
    returning to the shell's own group is proof the interception stopped it (tui
    itself is transient — cleared the instant the program dies — so we assert the
    stable end state, not the flash of tui=True)."""
    if shutil.which(prog) is None:
        pytest.skip(f"{prog} not installed")

    async def scenario():
        core = _kernel(tmp_path)
        inst = await core.open_window(100, ShellTopic, "work")
        buf = tmp_path / "buf.txt"
        buf.write_text("hello\nworld\n")
        await core.handle_message(_msg(template.format(f=buf), inst.thread_id))
        shell_pg = os.getpgid(inst.proc.pid)
        # would stay on the TUI's own group forever if it were never intercepted
        assert await _until(lambda: os.tcgetpgrp(inst.master_fd) == shell_pg, timeout=12.0)
        assert await _until(lambda: inst.tui is False, timeout=2.0)  # panel handed back
        assert inst.allow_universal is True
        await inst._kill()

    asyncio.run(scenario())


def test_clear_command_is_not_intercepted(tmp_path):
    """A plain `clear` emits only `?2J` and hands the terminal straight back — it must
    not read as a TUI (the false-positive side of the `top`/`?1h` detection)."""

    async def scenario():
        core = _kernel(tmp_path)
        inst = await core.open_window(100, ShellTopic, "work")
        await core.handle_message(_msg("clear", inst.thread_id))
        await asyncio.sleep(1.0)  # let the clear stream through
        assert inst.tui is False
        assert inst.allow_universal is True
        await inst._kill()

    asyncio.run(scenario())


def test_tui_intercept_posts_notice(tmp_path):
    """A detected TUI posts a persistent notice from the kill path, not the live panel:
    _end_tui clears tui within ~100ms, faster than a flush render could ever catch a
    tui=True frame, so a notice edited into the panel was a no-op the user never saw."""

    async def scenario():
        core = _kernel(tmp_path)
        inst = await core.open_window(100, ShellTopic, "work")
        core.bot.sent.clear()
        cmd = (
            "python3 -c 'import signal,sys,time; signal.signal(signal.SIGTERM, "
            'lambda *a: sys.exit(0)); sys.stdout.write("\\033[?1049h"); '
            "sys.stdout.flush(); time.sleep(30)'"
        )
        await core.handle_message(_msg(cmd, inst.thread_id))
        # MarkdownV2 escaping mangles "full-screen"; "terminal UI" survives clean
        assert await _until(lambda: any("terminal UI" in t for t in core.bot.sent))
        await inst._kill()

    asyncio.run(scenario())


def test_tui_process_is_stopped_and_shell_is_handed_back(tmp_path):
    async def scenario():
        core = _kernel(tmp_path)
        inst = await core.open_window(100, ShellTopic, "work")
        # a program that enters the alt-screen and would otherwise block forever, and
        # (like nano) never emits the alt-screen-off sequence when signalled
        cmd = (
            "python3 -c 'import signal,sys,time; signal.signal(signal.SIGTERM, "
            'lambda *a: sys.exit(0)); sys.stdout.write("\\033[?1049h"); '
            "sys.stdout.flush(); time.sleep(30)'"
        )
        await core.handle_message(_msg(cmd, inst.thread_id))
        assert await _until(lambda: inst.tui)  # detected the alt-screen
        shell_pg = os.getpgid(inst.proc.pid)
        # the process is stopped and the shell's own group holds the terminal again
        assert await _until(lambda: os.tcgetpgrp(inst.master_fd) == shell_pg, timeout=6.0)
        # and the panel is handed back (not stranded on the notice card)
        assert await _until(lambda: inst.tui is False, timeout=6.0)
        assert inst.allow_universal is True
        await inst._kill()

    asyncio.run(scenario())
