"""Shell plugin: `/shell [name]` opens a window with a persistent PTY-backed shell.

State sticks across messages (cd, env) and interactive prompts (sudo, ssh, a REPL)
can be answered — each plain message in the window is a line to stdin. Slashes are
raw input (not commands); the panel's Ctrl-C / Ctrl-D / close buttons drive it.
Output streams line-based (ANSI stripped) into one live message; a full-screen TUI
(vim, htop) can't render there, so it's detected and its process is stopped (the
panel can't show it). The window survives a bot restart: the PTY is gone, so the
next message revives a fresh shell in the last working directory. The chosen
workspace root and last cwd persist in the window store.
"""

from __future__ import annotations

import asyncio
import fcntl
import glob
import os
import pty
import re
import signal
import struct
import termios
from pathlib import Path

from tgforge.base import ui
from tgforge.base.kernel import Plugin, Topic, action, launch, on_message, pid_cwd, reap

# ── PtyWindow: a Topic backed by a PTY-hosted child ────────────────
# Owns the spawn / non-blocking read / write / reap plumbing. The child is reaped in
# exactly one place — `_kill` waits after the signal — so no window can leak its
# process. A subclass sets the pty size, builds the env, and implements `_on_output`
# plus its own render; `_on_kill` cancels any aux task.

READ_SIZE = 65536


class PtyWindow(Topic):
    pty_rows: int = 40
    pty_cols: int = 120

    def __init__(self, core, thread_id, name, saved):
        super().__init__(core, thread_id, name, saved)
        self.master_fd = -1
        self.proc: asyncio.subprocess.Process | None = None
        self.closed = False

    async def _spawn_pty(self, cwd, env, argv=("/bin/bash", "--norc", "-i")) -> None:
        """Open a PTY, launch `argv` as its session leader, and start reading. The
        subclass calls this from its own `_spawn`, then starts its render task."""
        master, slave = pty.openpty()
        winsize = struct.pack("HHHH", self.pty_rows, self.pty_cols, 0, 0)
        fcntl.ioctl(master, termios.TIOCSWINSZ, winsize)

        def _pre():
            os.setsid()
            fcntl.ioctl(0, termios.TIOCSCTTY, 0)

        self.proc = await self.spawn(
            *argv, stdin=slave, stdout=slave, stderr=slave, cwd=cwd, env=env, preexec_fn=_pre
        )
        os.close(slave)
        self.master_fd = master
        self.closed = False
        os.set_blocking(master, False)
        asyncio.get_event_loop().add_reader(master, self._on_read)

    def _on_read(self) -> None:
        try:
            data = os.read(self.master_fd, READ_SIZE)
        except (BlockingIOError, InterruptedError):
            return
        except OSError:
            data = b""  # EIO: the child exited and the pty closed
        if not data:
            asyncio.get_event_loop().call_soon(lambda: asyncio.ensure_future(self._kill()))
            return
        self._on_output(data)

    def _on_output(self, data: bytes) -> None:
        """Handle one chunk of raw PTY output. Subclass implements."""
        raise NotImplementedError

    def _write(self, data: bytes) -> None:
        if self.master_fd >= 0:
            try:
                os.write(self.master_fd, data)
            except OSError:
                pass

    async def _kill(self) -> None:
        if self.closed:
            return
        self.closed = True
        if self.master_fd >= 0:
            try:
                asyncio.get_event_loop().remove_reader(self.master_fd)
            except (OSError, ValueError):
                pass
            os.close(self.master_fd)
            self.master_fd = -1
        if self.proc:
            await reap(self.proc)  # the one reaper — kill if running, then wait
        await self._on_kill()

    async def _on_kill(self) -> None:
        """Subclass hook: cancel any auxiliary task once the child is reaped."""


FLUSH_INTERVAL = 1.5
TAIL_CHARS = 3800

_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")
_C0 = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
# private-mode markers a full-screen/interactive TUI turns on: alt-screen (vim,
# htop, less), focus-tracking / theme-sync (Ink apps, which never switch the
# alt-screen), OR application cursor-keys (`?1h`, DECCKM) — the tell for `top`, which
# paints via clear-screen + cursor addressing and never enters the alt-screen. `?1h`
# separates it cleanly from a plain `clear` (emits only `?2J`) and from readline,
# which uses bracketed paste (`?2004h`, left out on purpose) not `?1h`. Detected on
# the RAW stream; the plain shell here emits none of these.
_TUI_ON = ("\x1b[?1049h", "\x1b[?1047h", "\x1b[?47h", "\x1b[?1004h", "\x1b[?2031h", "\x1b[?1h")
_TUI_OFF = ("\x1b[?1049l", "\x1b[?1047l", "\x1b[?47l", "\x1b[?1004l", "\x1b[?2031l", "\x1b[?1l")


def _clean(text: str) -> str:
    """Strip ANSI + stray C0; apply carriage-return overwrites line by line."""
    text = _ANSI.sub("", text).replace("\r\n", "\n")
    out = []
    for line in text.split("\n"):
        if "\r" in line:
            line = line.split("\r")[-1]
        out.append(line)
    return _C0.sub("", "\n".join(out))


@launch("/shell", "open an interactive shell window")
class ShellTopic(PtyWindow):
    id = "shell"
    icon = "🖥"
    menu_label = "Shell"
    slash_commands = False  # slashes are shell input; controls are the panel buttons

    def __init__(self, core, thread_id, name, saved):
        super().__init__(core, thread_id, name, saved)
        self.acc = ""  # accumulated cleaned output
        self.dirty = False
        self.tui = False  # a full-screen app holds the alt-screen; panel can't render it
        self.allow_universal = True  # flipped off while a TUI holds the screen
        self.last_cmd = ""  # last line fed — names the program that went full-screen
        self._raw_tail = ""
        self.msg_id: int | None = None
        self._last_panel: str | None = None  # last rendered panel md — skip no-op edits
        self._flush_task: asyncio.Task | None = None

    # ── Lifecycle ──────────────────────────────────────────────────
    def title_suffix(self) -> str | None:
        cwd = self.saved.get("cwd")
        return (Path(cwd).name or cwd) if cwd else None

    async def on_open(self):
        cwd = await self._pick_cwd()
        self.saved["cwd"] = cwd
        await self._spawn(cwd)
        await self.send("🖥 interactive shell — send commands as messages")
        await self.refresh_title()

    async def on_revive(self):
        pass  # the PTY is gone; the next message revives a fresh shell lazily

    async def on_close(self):
        await self._kill()

    async def _pick_cwd(self) -> str:
        roots = self.plugin.workspace_roots() if self.plugin else []
        if not roots:
            return str(self._core.config.home_path)
        if len(roots) == 1:
            return str(roots[0])
        choice = await self.menu("📁 Workspace", [(p.name, str(p)) for p in roots])
        return choice or str(roots[0])

    # ── PTY ────────────────────────────────────────────────────────
    async def _spawn(self, cwd: str | None):
        env = {
            **os.environ,
            # a real terminal so a TUI (htop, vim, less) enters the alt-screen — the
            # `\x1b[?1049h` the reader watches for to intercept it. `dumb` suppressed
            # that sequence, so a TUI drew cursor-addressed garbage into the panel.
            "TERM": "xterm-256color",
            "LANG": os.environ.get("LANG") or "C.UTF-8",
            "PS1": r"\w \$ ",
            "PATH": f"{Path.home()}/.local/bin:" + os.environ.get("PATH", ""),
        }
        await self._spawn_pty(cwd or str(self._core.config.home_path), env)
        self._flush_task = asyncio.create_task(self._flush_loop())

    async def _ensure_proc(self):
        if self.proc is None or self.proc.returncode is not None:
            cwd = self.saved.get("cwd")
            where = f", back in {cwd}" if cwd else ""
            await self._spawn(cwd)
            await self.send(f"🖥 shell restarted — fresh process{where}")

    def _on_output(self, data: bytes):
        raw = data.decode(errors="replace")
        scan = self._raw_tail + raw
        self._raw_tail = scan[-16:]
        if not self.tui and any(s in scan for s in _TUI_ON):
            self.tui = True
            self.allow_universal = False  # a full-screen app owns the window
            asyncio.ensure_future(self._end_tui())  # notify, kill it, hand the shell back
            return
        if self.tui:
            if any(s in scan for s in _TUI_OFF):
                self.tui = False
                self.allow_universal = True
                self.acc = ""
                self.dirty = True
            return
        self.acc = (self.acc + _clean(raw))[-TAIL_CHARS * 2 :]
        self.dirty = True

    async def _flush_loop(self):
        while not self.closed:
            await asyncio.sleep(FLUSH_INTERVAL)
            if self.dirty:
                self.dirty = False
                await self._render()
                cwd = self._pid_cwd()
                if cwd and self.saved.get("cwd") != cwd:
                    self.saved["cwd"] = cwd  # snapshot so a revive lands here
                    await self.refresh_title()

    def _signal_foreground(self, sig: int) -> bool:
        """Signal the PTY's foreground process group (the TUI), never the shell
        itself. Returns False if the shell's own group already holds the terminal
        (nothing to signal)."""
        if self.master_fd < 0 or self.proc is None:
            return False
        try:
            fg = os.tcgetpgrp(self.master_fd)
            if fg > 0 and fg != os.getpgid(self.proc.pid):
                os.killpg(fg, sig)
                return True
        except OSError:
            pass
        return False

    def _foreground_is_shell(self) -> bool:
        if self.master_fd < 0 or self.proc is None:
            return True
        try:
            return os.tcgetpgrp(self.master_fd) == os.getpgid(self.proc.pid)
        except OSError:
            return True  # can't tell → assume the child is gone

    async def _end_tui(self):
        """A TUI was detected; the panel can't render it. Post a one-shot notice as its
        own message (the live panel returns to the shell, so a notice edited into it
        would vanish before it's read), then SIGTERM its process group, escalate to
        SIGKILL if it clings, and reset the panel — we do NOT wait for the app to emit
        the alt-screen-off sequence (nano and friends never do), so the shell is always
        handed back rather than stranded."""
        cmd = self.last_cmd or "a program"
        asyncio.ensure_future(
            self.send_rich(
                f"⚠️ `{cmd}` opened a full-screen terminal UI — this panel streams line "
                "output only, so it was closed. Run a full-screen app on the PC instead."
            )
        )
        self._signal_foreground(signal.SIGTERM)
        for sig in (None, signal.SIGKILL):
            if sig is not None and not self._foreground_is_shell():
                self._signal_foreground(sig)
            for _ in range(15):  # ~1.5s grace per stage
                if self._foreground_is_shell():
                    break
                await asyncio.sleep(0.1)
            if self._foreground_is_shell():
                break
        self.tui = False
        self.allow_universal = True
        self.acc = ""
        self.dirty = True
        self._write(b"\n")  # nudge bash to draw a fresh prompt into the panel

    def _pid_cwd(self) -> str | None:
        if self.proc is None:
            return None
        return pid_cwd(self.proc.pid)

    async def _render(self):
        if self.tui:
            return  # a full-screen app briefly holds the screen; _end_tui posts the notice
        body = self.acc[-TAIL_CHARS:].strip() or "(no output yet)"
        esc = body.replace("\\", "\\\\").replace("`", "\\`")
        await self._push(f"```\n{esc}\n```", body)

    async def _push(self, md: str, plain: str):
        if self.msg_id is None:
            self.msg_id = await self.send(plain)
            if self.msg_id is None:
                return
        if md == self._last_panel:
            return  # unchanged → skip the no-op edit that would flip the panel to plain
        self._last_panel = md
        await self.edit_md(self.msg_id, md, plain, self._keys())

    def _keys(self):
        return ui.keyboard(
            [
                [
                    ui.act("Ctrl-C", "sh", "int"),
                    ui.act("Ctrl-D", "sh", "eof"),
                    ui.act("close", "sh", "close"),
                ]
            ]
        )

    # ── Handlers ───────────────────────────────────────────────────
    @on_message
    async def feed(self, ctx):
        await self._ensure_proc()
        self.dirty = True  # the shell echoes the line back; don't add our own
        # the raw message, not ctx.text: the router strips leading/trailing space, which
        # would corrupt a password or any input where whitespace is significant
        raw = ctx.message.text if (ctx.message and ctx.message.text is not None) else ctx.text
        self.last_cmd = raw.strip()
        self._write((raw + "\n").encode())
        if ctx.message is not None:
            await ctx.delete(ctx.message.message_id)  # the panel shows it

    @action("sh")
    async def button(self, ctx, arg):
        if arg == "close":
            await self.close()
        else:
            self._write({"int": b"\x03", "eof": b"\x04"}.get(arg, b""))

    async def _on_kill(self):
        if self._flush_task:
            self._flush_task.cancel()


class Shell(Plugin):
    id = "shell"
    topics = [ShellTopic]

    def __init__(self, roots=None):
        self._roots = list(roots) if roots else []

    def workspace_roots(self) -> list[Path]:
        """Configured workspace roots (globs → existing dirs). Persisted edits in
        the plugin store take priority; empty means the window falls back to home."""
        globs = self.saved.get("roots", None) if hasattr(self, "saved") else None
        globs = globs if globs is not None else self._roots
        out: list[Path] = []
        for g in globs:
            for hit in sorted(glob.glob(os.path.expanduser(g))):
                p = Path(hit)
                if p.is_dir() and p not in out:
                    out.append(p)
        return out
