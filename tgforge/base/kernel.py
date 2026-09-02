"""The Kernel — the runtime plugins register into, an operating system for windows.

This module is the whole framework core: the process probes + subprocess reaper,
persistence (`AppDB`, one SQLite file; `SavedDict`, namespaced dict-like views),
the plugin declaration decorators, the `Topic` and `Plugin` bases, the `Transport`
(thin aiogram send/edit wrappers with MarkdownV2 fallback, chunking, flood-wait
handling), the command registry (built once at startup; aborts the launch on any
name clash), the per-call `Context` a handler receives, and the `Kernel` itself.
The kernel owns the chain-of-responsibility router, the owner binding, window
ownership and lifecycle (open / adopt / release / close), the interaction helpers
(button/text prompts), cross-plugin services, daily usage counts, and the reconcile
sweep that drops manually-deleted topics. A plugin never receives the kernel — it
works through a per-call `Context` and its `Topic` instance. The built-in commands
(`/`, `/new`, `/close`, `/usage`, `/help`, `/init`) ride an implicit plugin
(`_CorePlugin`) declared here, so routing never special-cases them.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import datetime
import json
import logging
import mimetypes
import os
import sqlite3
import subprocess
import sys
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

from aiogram import Bot as AioBot
from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InputMediaPhoto,
    Message,
    ReplyParameters,
)

from tgforge.base import ui
from tgforge.base.config import BotConfig
from tgforge.base.ui import MAX_MSG, chunks, md_chunks, to_md

LOGGER = logging.getLogger("tgforge")

Fallback = Callable[[str], Awaitable[None]]
RECONCILE_INTERVAL = 300  # seconds between manual-deletion sweeps
MENU_BACK = "menu:back"  # the Back option value in an interactive menu
CANCEL = "__cancel__"  # the auto-appended Cancel option value in a confirm; resolves to None


# ── Process probes + the subprocess reaper ─────────────────────────
# Linux reads /proc directly (fast, no fork); macOS shells out to lsof (ships with
# the OS). Any other platform degrades to a safe default rather than raising.

IS_LINUX = sys.platform.startswith("linux")
IS_MACOS = sys.platform == "darwin"


async def reap(proc) -> None:
    """Kill an asyncio subprocess if still running, then wait for it — so its transport
    closes and no child leaks. Safe if it already exited (the child watcher may reap it
    first, racing a bare kill to ProcessLookupError). The one reaper for every spawn."""
    if proc.returncode is None:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
    await proc.wait()


def pid_cwd(pid: int) -> str | None:
    """Working directory of a live process, or None if it can't be read."""
    if IS_LINUX:
        try:
            import os

            return os.readlink(f"/proc/{pid}/cwd")
        except OSError:
            return None
    if IS_MACOS:
        try:
            out = subprocess.run(
                ["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"],
                capture_output=True,
                text=True,
                timeout=5,
            ).stdout
        except (OSError, subprocess.SubprocessError):
            return None
        for line in out.splitlines():
            if line.startswith("n"):
                return line[1:]
    return None


def file_held_open(path: str) -> bool:
    """True if any process holds `path` open. On platforms with no probe, returns
    True — the conservative answer, since callers use a False to declare a process
    gone and a false negative would end a live job early."""
    if IS_LINUX:
        import os

        try:
            fd_dirs = list(Path("/proc").glob("[0-9]*/fd"))
        except OSError:
            return False
        for fd_dir in fd_dirs:
            try:
                fds = list(fd_dir.iterdir())
            except OSError:
                continue  # the process exited mid-scan
            for fd in fds:
                try:
                    if os.readlink(fd) == path:
                        return True
                except OSError:
                    continue  # this fd closed mid-scan; keep checking the rest
        return False
    if IS_MACOS:
        try:
            return (
                subprocess.run(
                    ["lsof", "--", path],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=5,
                ).returncode
                == 0
            )
        except (OSError, subprocess.SubprocessError):
            return True
    return True


# ── Persistence: SavedDict views over the one SQLite file (AppDB) ──
# Plugins never open, read, or write a file — the kernel hands them a `SavedDict`
# bound to a namespace (core / per-plugin / per-window) and they treat it as a dict
# of JSON-serializable values. Every assignment is a single-row upsert + commit, so
# a write is atomic and crash-safe. A separate `usage` table holds daily counts.


_MISSING = object()


def core_ns() -> str:
    return "core"


def plugin_ns(plugin_id: str) -> str:
    return f"plugin:{plugin_id}"


def window_ns(class_id: str, thread_id: int) -> str:
    return f"win:{class_id}:{thread_id}"


class SavedDict:
    """A dict-like view of one namespace. Values must be JSON-serializable."""

    def __init__(self, db: AppDB, namespace: str):
        self._db = db
        self._ns = namespace

    def __getitem__(self, key: str):
        val = self.get(key, _MISSING)
        if val is _MISSING:
            raise KeyError(key)
        return val

    def get(self, key: str, default=None):
        row = self._db.conn.execute(
            "SELECT value FROM store WHERE namespace = ? AND key = ?", (self._ns, key)
        ).fetchone()
        return default if row is None else json.loads(row[0])

    def __setitem__(self, key: str, value) -> None:
        self._db.conn.execute(
            "INSERT INTO store(namespace, key, value) VALUES (?, ?, ?) "
            "ON CONFLICT(namespace, key) DO UPDATE SET value = excluded.value",
            (self._ns, key, json.dumps(value)),
        )
        self._db.conn.commit()

    def __delitem__(self, key: str) -> None:
        self._db.conn.execute("DELETE FROM store WHERE namespace = ? AND key = ?", (self._ns, key))
        self._db.conn.commit()

    def pop(self, key: str, default=_MISSING):
        val = self.get(key, _MISSING)
        if val is _MISSING:
            if default is _MISSING:
                raise KeyError(key)
            return default
        del self[key]
        return val

    def __contains__(self, key: str) -> bool:
        return self.get(key, _MISSING) is not _MISSING

    def setdefault(self, key: str, default=None):
        val = self.get(key, _MISSING)
        if val is _MISSING:
            self[key] = default
            return default
        return val

    def keys(self) -> list[str]:
        rows = self._db.conn.execute(
            "SELECT key FROM store WHERE namespace = ? ORDER BY key", (self._ns,)
        ).fetchall()
        return [r[0] for r in rows]

    def items(self):
        rows = self._db.conn.execute(
            "SELECT key, value FROM store WHERE namespace = ? ORDER BY key", (self._ns,)
        ).fetchall()
        return [(k, json.loads(v)) for k, v in rows]

    def as_dict(self) -> dict:
        return dict(self.items())

    def clear(self) -> None:
        self._db.drop(self._ns)


class AppDB:
    """The one SQLite file behind every store. Owns the schema and namespace ops."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path))
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS store("
            "namespace TEXT, key TEXT, value TEXT, PRIMARY KEY(namespace, key))"
        )
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS usage("
            "day TEXT, scope TEXT, name TEXT, count INTEGER, PRIMARY KEY(day, scope, name))"
        )
        self.conn.commit()

    def saved(self, namespace: str) -> SavedDict:
        return SavedDict(self, namespace)

    def drop(self, namespace: str) -> None:
        """Remove every row of a namespace (a window closed or was deleted)."""
        self.conn.execute("DELETE FROM store WHERE namespace = ?", (namespace,))
        self.conn.commit()

    def namespaces(self, prefix: str = "") -> list[str]:
        """Distinct namespaces (optionally by prefix) — used to rebuild windows."""
        rows = self.conn.execute(
            "SELECT DISTINCT namespace FROM store WHERE namespace LIKE ? ORDER BY namespace",
            (prefix + "%",),
        ).fetchall()
        return [r[0] for r in rows]

    def bump(self, day: str, scope: str, name: str, by: int = 1) -> None:
        """Add to a daily usage counter (one upsert)."""
        self.conn.execute(
            "INSERT INTO usage(day, scope, name, count) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(day, scope, name) DO UPDATE SET count = count + excluded.count",
            (day, scope, name, by),
        )
        self.conn.commit()

    def usage_totals(self) -> list[tuple[str, str, int]]:
        """(scope, name, total) across all days, most-used first — for /usage."""
        rows = self.conn.execute(
            "SELECT scope, name, SUM(count) AS c FROM usage "
            "GROUP BY scope, name ORDER BY c DESC, scope, name"
        ).fetchall()
        return [(s, n, c) for s, n, c in rows]

    def close(self) -> None:
        self.conn.close()


# ── Plugin declaration decorators ───────────────────────────────────
# Each decorator only tags the function/class with a `_tg_*` marker; the registry
# reads the markers at startup. A handler returns False to decline an event so the
# router falls through (any other return, including None, consumes it).


def universal(
    name: str,
    help: str = "",
    icon: str = "",
    menu: str = "any",
    inline: bool = False,
    aliases: tuple[str, ...] = (),
):
    """A command callable in any window (untagged) and in General (mention-only).
    `icon` is its glyph in the tap menu (a fallback is used if empty). `menu` sets
    where it appears: "any" everywhere, "window" only inside a window (needs a
    topic, e.g. /close, /restart), "none" never (bootstrap/meta, e.g. /init).
    `inline=True` means the handler returns its output as text: typed, it is sent;
    tapped from the menu, it renders in place with a Back button. A non-inline
    command does its own output and the menu marks it as triggered (no Back).
    `aliases` are extra names that route to the same handler."""

    def deco(fn):
        fn._tg_universal = (name, help, icon, menu, inline, tuple(aliases))
        return fn

    return deco


def command(
    name: str,
    help: str = "",
    icon: str = "",
    inline: bool = False,
    aliases: tuple[str, ...] = (),
):
    """A class-scoped command, valid only inside a window of the owning class.
    `icon` is its glyph in the window's tap menu; without one a fallback is used.
    `inline` / `aliases` behave as on `universal`."""

    def deco(fn):
        fn._tg_command = (name, help, icon, inline, tuple(aliases))
        return fn

    return deco


def on_message(fn):
    """A class's plain-text catch handler (sees every event; may return PASS)."""
    fn._tg_on_message = True
    return fn


def on_unknown(fn):
    """A class's handler for a slash nothing above consumed (may return PASS)."""
    fn._tg_on_unknown = True
    return fn


def action(name: str):
    """An inline-button callback; a tap with data ``act:<name>:<arg>`` routes here."""

    def deco(fn):
        fn._tg_action = name
        return fn

    return deco


def prefix(char: str, help: str = ""):
    """A universal text-prefix handler (e.g. claude's ``!`` / ``!!``)."""

    def deco(fn):
        fn._tg_prefix = (char, help)
        return fn

    return deco


def service(name: str):
    """A cross-plugin capability callable by name via ``ctx.call_service`` —
    invoked as ``handler(ctx, *args)`` with a context for the target thread."""

    def deco(fn):
        fn._tg_service = name
        return fn

    return deco


def launch(name: str, help: str = "", aliases: tuple[str, ...] = ()):
    """A `Topic` class's creator command, with context-dependent behavior handled
    by the router: new window in General, new-or-transform in a core window,
    ignored elsewhere. `aliases` are extra names that open the same class."""

    def deco(cls):
        cls._tg_launch = (name, help, tuple(aliases))
        return cls

    return deco


# ── Topic: one window class; each open window is one instance ──────


class Topic:
    id: str = ""  # unique class id (also the store's class segment)
    allow_universal: bool = True  # may another plugin's universals run here
    slash_commands: bool = True  # False → slashes are raw input (a shell), not commands
    icon: str = ""  # optional glyph for menus/titles (the class owns its own look)
    menu_label: str = ""  # optional display name in the launcher menu (else id.title())

    def command_allowed(self, kind: str, name: str) -> bool:
        """Whether a command may appear and run in this window — the plugin's full say
        over which commands run here, core universals included. `kind` is 'class',
        'universal', or 'launch'. Default: all allowed; override to restrict."""
        return True

    @classmethod
    def menu_entry(cls) -> str:
        """The class's own button text in the launcher menu — icon + name, both
        declared by the class so the kernel bakes in no plugin identities."""
        return f"{cls.icon} {cls.menu_label or cls.id.title()}".strip()

    @classmethod
    def extend(cls, ext):
        """Merge another class's decorated command/action/catch handlers onto this
        class (used by a plugin that imports and extends another's class). A method
        whose attribute name already exists here is a conflict, raised now."""
        markers = ("_tg_command", "_tg_action", "_tg_on_message", "_tg_on_unknown")
        for attr, fn in list(vars(ext).items()):
            if callable(fn) and any(hasattr(fn, m) for m in markers):
                if attr in cls.__dict__:
                    raise ValueError(f"{cls.__name__}.extend: {attr!r} already defined")
                setattr(cls, attr, fn)
        return ext

    def __init__(self, core, thread_id: int, name: str, saved: SavedDict):
        self._core = core
        self.thread_id = thread_id
        self.name = name
        self.saved = saved
        self.plugin = None  # the owning Plugin (set by the kernel); for its store/roots
        self._procs: set = set()  # every subprocess this window spawned; reaped on close

    # ── Subprocess ownership ───────────────────────────────────────
    async def spawn(self, *args, **kwargs):
        """Spawn a child and track it, so the kernel reaps it when this window closes.
        Every window subprocess goes through here — a kill elsewhere is just a signal;
        reaping is guaranteed centrally, so no child can leak by a forgotten wait."""
        self._procs = {p for p in self._procs if p.returncode is None}  # drop reaped ones
        proc = await asyncio.create_subprocess_exec(*args, **kwargs)
        self._procs.add(proc)
        return proc

    async def reap_all(self) -> None:
        """Reap every subprocess this window spawned. Called by the kernel after close;
        idempotent — an already-exited child's wait returns at once."""
        for proc in list(self._procs):
            await reap(proc)
        self._procs.clear()

    def title_suffix(self) -> str | None:
        """The dynamic tail of this window's title (session name, folder, cwd, …).
        None → the bare class label. Override, and call `refresh_title()` on change."""
        return None

    async def refresh_title(self) -> None:
        """Recompose the topic title from the current `title_suffix()`; the rename is
        skipped when unchanged, so a burst of state changes stays flood-safe."""
        await self._core.refresh_title(self.thread_id)

    # ── Lifecycle hooks (override as needed) ───────────────────────
    async def on_open(self) -> None:
        """A fresh window was just opened."""

    async def on_revive(self) -> None:
        """The bot restarted; `self.saved` is already populated."""

    async def on_close(self) -> None:
        """The window is going away (released, closed, or found deleted)."""

    async def on_shutdown(self) -> None:
        """The bot is stopping for a restart (not the window closing). Settle any live
        UI so nothing is left mid-render; the window revives after the restart."""

    async def on_restarted(self) -> None:
        """This window asked for the just-completed restart; resume however it needs to.
        Fires only on the requester topic, after revive. Default: nothing."""

    # ── Window control ─────────────────────────────────────────────
    async def release(self) -> None:
        """Return this window to the `core` class, keeping its history."""
        await self._core.release_window(self.thread_id)

    async def close(self) -> None:
        """Delete this window."""
        await self._core.close_window(self.thread_id)

    async def rename(self, name: str) -> None:
        await self._core.rename_topic(self.thread_id, name)
        self.name = name

    # ── Replies bound to this window's thread ──────────────────────
    async def send(self, text, reply_to=None):
        return await self._core.send(self._core.chat_id, text, self.thread_id, reply_to=reply_to)

    async def send_rich(self, text, reply_to=None):
        return await self._core.send_rich(
            self._core.chat_id, text, self.thread_id, reply_to=reply_to
        )

    async def edit(self, msg_id, text, reply_markup=None):
        return await self._core.edit(self._core.chat_id, msg_id, text, reply_markup=reply_markup)

    async def edit_md(self, msg_id, md, plain, reply_markup=None):
        return await self._core.edit_md(
            self._core.chat_id, msg_id, md, plain, reply_markup=reply_markup
        )

    async def edit_rich(self, msg_id, text):
        return await self._core.edit_rich(self._core.chat_id, msg_id, text)

    async def set_markup(self, msg_id, reply_markup):
        """Attach/replace an inline keyboard on an existing message."""
        await self._core._call(
            lambda: self._core.bot.edit_message_reply_markup(
                chat_id=self._core.chat_id, message_id=msg_id, reply_markup=reply_markup
            )
        )

    async def send_file(self, path):
        return await self._core.send_file(self._core.chat_id, path, self.thread_id)

    async def send_photo(self, media_type, b64, reply_markup=None):
        return await self._core.send_photo_b64(
            self._core.chat_id, media_type, b64, self.thread_id, reply_markup=reply_markup
        )

    async def edit_photo(self, msg_id, media_type, b64, reply_markup=None):
        return await self._core.edit_photo_b64(
            self._core.chat_id, msg_id, media_type, b64, reply_markup=reply_markup
        )

    async def delete(self, msg_id):
        await self._core.delete_msg(self._core.chat_id, msg_id)

    async def ask_buttons(self, text, options, timeout=300, announce=None, cancel=True):
        return await self._core.ask_buttons(
            self._core.chat_id,
            self.thread_id,
            text,
            options,
            timeout=timeout,
            announce=announce,
            cancel=cancel,
        )

    async def menu(self, title, options):
        """Single-choice menu; returns the chosen value (None if backed out). Nests as
        a submenu when opened from a menu tap. See `Context.menu`."""
        return await self._core.menu(self._core.chat_id, self.thread_id, title, options)

    async def ask_text(self, prompt=None, timeout=180):
        return await self._core.ask_text(
            self._core.chat_id, self.thread_id, prompt, timeout=timeout
        )

    async def pick_dir(self, start=None, title="📂 Pick a folder"):
        """Browse folders in one in-place message; return the chosen directory (None if
        cancelled). The shared path-picker every window inherits — a plugin needing a
        different tree writes its own loop the same way. Rides the menu tap primitive;
        navigation and Cancel are built in, so no free-typed paths."""
        page_size = 12
        cwd = os.path.abspath(os.path.expanduser(str(start or self._core.config.home_path)))
        page = 0
        msg_id = None
        while True:
            try:
                subdirs = sorted(
                    (e.name for e in os.scandir(cwd) if e.is_dir(follow_symlinks=True)),
                    key=str.lower,
                )
            except OSError:
                subdirs = []
            pages = max(1, (len(subdirs) + page_size - 1) // page_size)
            page = max(0, min(page, pages - 1))
            lo = page * page_size
            opts = [(f"✓ Use {os.path.basename(cwd) or '/'}", "use")]
            if cwd != "/":
                opts.append(("⬆ ..", "up"))
            opts += [(f"📁 {d}", f"cd:{d}") for d in subdirs[lo : lo + page_size]]
            if page > 0:
                opts.append(("‹ prev", "pg:-1"))
            if page < pages - 1:
                opts.append(("next ›", "pg:+1"))
            opts.append(("✖ Cancel", "cancel"))
            header = f"{title}\n{cwd}" + (f"  ·  p{page + 1}/{pages}" if pages > 1 else "")
            choice, msg_id = await self._core._prompt_tap(
                self._core.chat_id, self.thread_id, header, opts, msg_id
            )
            if choice in (None, "cancel", "use"):
                await self._core._strip_markup(self._core.chat_id, msg_id)
                return cwd if choice == "use" else None
            if choice == "up":
                cwd, page = os.path.dirname(cwd) or "/", 0
            elif choice == "pg:-1":
                page -= 1
            elif choice == "pg:+1":
                page += 1
            elif choice.startswith("cd:"):
                cwd, page = os.path.join(cwd, choice[3:]), 0


# ── Plugin base ─────────────────────────────────────────────────────


class Plugin:
    id: str = ""  # store namespace; defaults to the lowercased class name
    topics: list[type[Topic]] = []

    #: set by the core at registration
    saved: SavedDict

    @property
    def plugin_id(self) -> str:
        return self.id or type(self).__name__.lower()

    async def on_startup(self) -> None:
        """Optional: run once after the bot connects (background loops, seeding)."""

    @classmethod
    def extend(cls, ext):
        """Not on Plugin — extension targets a `Topic` class (see Topic.extend)."""
        raise TypeError("extend a Topic class, not a Plugin")


# ── Transport: aiogram send/edit wrappers ──────────────────────────


class Transport:
    def __init__(self, bot: AioBot):
        self.bot = bot

    async def _call(self, make_coro):
        """Issue an aiogram call and return its result. `make_coro` is a zero-arg factory
        (`lambda: self.bot.send_message(...)`) so a flood-wait retry can re-issue a fresh
        coroutine — awaiting the same one twice is a RuntimeError. Honors one flood-wait;
        swallows API errors, returning None."""
        for _ in range(2):
            try:
                return await make_coro()
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after)
            except TelegramAPIError as e:
                LOGGER.debug("telegram api error: %r", e)
                return None
        return None

    async def send(self, chat_id, text, thread_id=None, reply_to=None) -> int | None:
        mid = None
        for i, chunk in enumerate(chunks(text)):
            kw = {}
            if thread_id is not None:
                kw["message_thread_id"] = thread_id
            if reply_to is not None and i == 0:
                kw["reply_parameters"] = ReplyParameters(message_id=reply_to)
            msg = await self._call(
                lambda chunk=chunk, kw=kw: self.bot.send_message(chat_id, chunk[:MAX_MSG], **kw)
            )
            mid = msg.message_id if msg else mid
        return mid

    async def edit(self, chat_id, msg_id, text, reply_markup=None) -> bool:
        msg = await self._call(
            lambda: self.bot.edit_message_text(
                text[:MAX_MSG],
                chat_id=chat_id,
                message_id=msg_id,
                reply_markup=reply_markup,
            )
        )
        return msg is not None

    async def send_rich(self, chat_id, text, thread_id=None, reply_to=None) -> int | None:
        mid = None
        for i, raw in enumerate(md_chunks(text)):
            kw = {}
            if thread_id is not None:
                kw["message_thread_id"] = thread_id
            if reply_to is not None and i == 0:
                kw["reply_parameters"] = ReplyParameters(message_id=reply_to)
            msg = await self._call(
                lambda raw=raw, kw=kw: self.bot.send_message(
                    chat_id, to_md(raw)[:MAX_MSG], parse_mode="MarkdownV2", **kw
                )
            )
            if msg is None:
                msg = await self._call(
                    lambda raw=raw, kw=kw: self.bot.send_message(chat_id, raw[:MAX_MSG], **kw)
                )
            mid = msg.message_id if msg else mid
        return mid

    async def edit_rich(self, chat_id, msg_id, text) -> bool:
        msg = await self._call(
            lambda: self.bot.edit_message_text(
                to_md(text)[:MAX_MSG],
                chat_id=chat_id,
                message_id=msg_id,
                parse_mode="MarkdownV2",
            )
        )
        if msg is not None:
            return True
        return await self.edit(chat_id, msg_id, text)

    async def edit_md(self, chat_id, msg_id, md, plain, reply_markup=None) -> bool:
        msg = await self._call(
            lambda: self.bot.edit_message_text(
                md[:MAX_MSG],
                chat_id=chat_id,
                message_id=msg_id,
                parse_mode="MarkdownV2",
                reply_markup=reply_markup,
            )
        )
        if msg is not None:
            return True
        return await self.edit(chat_id, msg_id, plain, reply_markup=reply_markup)

    async def delete_msg(self, chat_id, msg_id):
        await self._call(lambda: self.bot.delete_message(chat_id, msg_id))

    async def send_photo_b64(self, chat_id, media_type, b64, thread_id=None, reply_markup=None):
        raw = base64.b64decode(b64)
        if len(raw) > 10 * 1024 * 1024:
            return None
        ext = media_type.split("/")[-1] or "png"
        kw = {"message_thread_id": thread_id} if thread_id is not None else {}
        if reply_markup is not None:
            kw["reply_markup"] = reply_markup
        msg = await self._call(
            lambda: self.bot.send_photo(chat_id, BufferedInputFile(raw, f"image.{ext}"), **kw)
        )
        return msg.message_id if msg else None

    async def edit_photo_b64(self, chat_id, msg_id, media_type, b64, reply_markup=None) -> bool:
        """Replace an existing photo message's image in place (keeps one live frame
        instead of a growing pile). Returns False if the edit was rejected."""
        raw = base64.b64decode(b64)
        if len(raw) > 10 * 1024 * 1024:
            return False
        ext = media_type.split("/")[-1] or "png"
        media = InputMediaPhoto(media=BufferedInputFile(raw, f"image.{ext}"))
        msg = await self._call(
            lambda: self.bot.edit_message_media(
                media=media, chat_id=chat_id, message_id=msg_id, reply_markup=reply_markup
            )
        )
        return msg is not None

    async def download_file(self, file_id: str, dest: Path) -> Path | None:
        """Download a Telegram file (photo/document) to `dest`. Returns the path, or
        None on failure (e.g. over the 20MB bot-API download cap)."""
        try:
            info = await self.bot.get_file(file_id)
            buf = await self.bot.download_file(info.file_path)
        except TelegramAPIError:
            LOGGER.warning("getFile failed for %s (over the 20MB bot limit?)", file_id)
            return None
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(buf.read())
        return dest

    async def send_file(self, chat_id, path: Path, thread_id=None):
        if not path.is_file():
            raise RuntimeError(f"no such file: {path}")
        size = path.stat().st_size
        if size > 50 * 1024 * 1024:
            raise RuntimeError(f"{path.name} is {size / 1e6:.0f}MB — over the 50MB cap")
        media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        kw = {"message_thread_id": thread_id} if thread_id is not None else {}
        data = BufferedInputFile(path.read_bytes(), path.name)
        if media_type.startswith("image/") and size <= 10 * 1024 * 1024:
            await self._call(lambda: self.bot.send_photo(chat_id, data, **kw))
        else:
            await self._call(lambda: self.bot.send_document(chat_id, data, **kw))


# ── Command registry ───────────────────────────────────────────────


class RegistryConflict(Exception):
    """Raised when two providers clash; the message names both and the clash."""


@dataclass
class ClassEntry:
    cls: type  # the Topic subclass
    plugin_id: str
    commands: dict[str, tuple] = field(default_factory=dict)  # name -> (attr, help, icon, inline)
    command_aliases: dict[str, str] = field(default_factory=dict)  # alias -> primary name
    actions: dict[str, str] = field(default_factory=dict)  # action name -> attr
    on_message: str | None = None  # attr name
    on_unknown: str | None = None  # attr name

    def resolve(self, cmd: str) -> str | None:
        """The primary command name for `cmd` (itself or an alias), else None."""
        if cmd in self.commands:
            return cmd
        return self.command_aliases.get(cmd)


@dataclass
class Registry:
    # universal-name space: launch + universal + prefix all share it (aliases too)
    universals: dict[str, tuple] = field(
        default_factory=dict
    )  # name -> (plugin_id, attr, help, icon, menu, inline)
    universal_aliases: dict[str, str] = field(default_factory=dict)  # alias -> primary name
    launches: dict[str, tuple] = field(default_factory=dict)  # name -> (class_id, help)
    launch_aliases: dict[str, str] = field(default_factory=dict)  # alias -> primary name
    prefixes: dict[str, tuple] = field(default_factory=dict)  # char -> (plugin_id, attr, help)
    classes: dict[str, ClassEntry] = field(default_factory=dict)  # class id -> entry
    launch_class: dict[str, type] = field(default_factory=dict)  # launch name -> Topic class
    services: dict[str, tuple] = field(default_factory=dict)  # service name -> (plugin_id, attr)

    def resolve_universal(self, cmd: str) -> str | None:
        if cmd in self.universals:
            return cmd
        return self.universal_aliases.get(cmd)

    def resolve_launch(self, cmd: str) -> str | None:
        if cmd in self.launches:
            return cmd
        return self.launch_aliases.get(cmd)

    def _claim_universal_name(self, name: str, who: str) -> None:
        prior = (
            (self.universals.get(name) and "universal")
            or (name in self.universal_aliases and "universal alias")
            or (name in self.launches and "launch")
            or (name in self.launch_aliases and "launch alias")
            or (name in self.prefixes and "prefix")
        )
        if prior:
            raise RegistryConflict(f"name {name!r} registered twice ({prior} vs {who})")


def _scan_class(cls: type, plugin_id: str) -> ClassEntry:
    entry = ClassEntry(cls=cls, plugin_id=plugin_id)
    for attr in dir(cls):
        fn = getattr(cls, attr)
        if not callable(fn):
            continue
        if hasattr(fn, "_tg_command"):
            name, help, icon, inline, aliases = fn._tg_command
            for n in (name, *aliases):
                if n in entry.commands or n in entry.command_aliases:
                    raise RegistryConflict(f"command {n!r} defined twice in class {cls.id!r}")
            entry.commands[name] = (attr, help, icon, inline)
            for a in aliases:
                entry.command_aliases[a] = name
        if hasattr(fn, "_tg_action"):
            entry.actions[fn._tg_action] = attr
        if hasattr(fn, "_tg_on_message"):
            entry.on_message = attr
        if hasattr(fn, "_tg_on_unknown"):
            entry.on_unknown = attr
    return entry


def build_registry(plugins: list) -> Registry:
    """Build the tables from constructed plugin instances (each with `plugin_id`
    and `topics`). Raises RegistryConflict on any clash."""
    reg = Registry()
    for plugin in plugins:
        pid = plugin.plugin_id
        # universal commands + prefixes declared on the plugin
        for attr in dir(plugin):
            fn = getattr(plugin, attr)
            if not callable(fn):
                continue
            if hasattr(fn, "_tg_universal"):
                name, help, icon, menu, inline, aliases = fn._tg_universal
                reg._claim_universal_name(name, f"universal in {pid}")
                reg.universals[name] = (pid, attr, help, icon, menu, inline)
                for a in aliases:
                    reg._claim_universal_name(a, f"alias of {name} in {pid}")
                    reg.universal_aliases[a] = name
            if hasattr(fn, "_tg_prefix"):
                char, help = fn._tg_prefix
                reg._claim_universal_name(char, f"prefix in {pid}")
                reg.prefixes[char] = (pid, attr, help)
            if hasattr(fn, "_tg_service"):
                sname = fn._tg_service
                if sname in reg.services:
                    raise RegistryConflict(f"service {sname!r} registered twice")
                reg.services[sname] = (pid, attr)
        # topic classes + their launch commands and class-scoped handlers
        for cls in getattr(plugin, "topics", []):
            if not cls.id:
                raise RegistryConflict(f"topic class {cls.__name__} has no id")
            if cls.id in reg.classes:
                raise RegistryConflict(f"class id {cls.id!r} registered twice")
            reg.classes[cls.id] = _scan_class(cls, pid)
            if hasattr(cls, "_tg_launch"):
                name, help, aliases = cls._tg_launch
                reg._claim_universal_name(name, f"launch for class {cls.id!r}")
                reg.launches[name] = (cls.id, help)
                reg.launch_class[name] = cls
                for a in aliases:
                    reg._claim_universal_name(a, f"alias of {name} for class {cls.id!r}")
                    reg.launch_aliases[a] = name
                    reg.launch_class[a] = cls
    return reg


# ── Per-call handler context ───────────────────────────────────────


class Context:
    def __init__(self, core, chat_id, thread_id, message, text, args, topic, saved, user_id=None):
        self._core = core
        self.chat_id = chat_id
        self.thread_id = thread_id
        self.message = message
        self.text = text
        self.args = args
        self.user_id = user_id  # the acting user's id (None for a service call)
        self.topic: Topic | None = topic
        self.saved: SavedDict = saved  # the per-plugin SavedDict of the handling plugin

    @property
    def is_admin(self) -> bool:
        """True if the acting user is the bound owner (the bot's admin)."""
        return self.user_id is not None and self.user_id == self._core.owner_id

    # ── Replies ────────────────────────────────────────────────────
    async def send(self, text, reply_to=None):
        return await self._core.send(self.chat_id, text, self.thread_id, reply_to=reply_to)

    async def send_rich(self, text, reply_to=None):
        return await self._core.send_rich(self.chat_id, text, self.thread_id, reply_to=reply_to)

    async def edit(self, msg_id, text, reply_markup=None):
        return await self._core.edit(self.chat_id, msg_id, text, reply_markup=reply_markup)

    async def edit_md(self, msg_id, md, plain, reply_markup=None):
        return await self._core.edit_md(self.chat_id, msg_id, md, plain, reply_markup=reply_markup)

    async def send_file(self, path):
        return await self._core.send_file(self.chat_id, path, self.thread_id)

    async def download_file(self, file_id, dest):
        return await self._core.download_file(file_id, dest)

    async def send_photo(self, media_type, b64):
        return await self._core.send_photo_b64(self.chat_id, media_type, b64, self.thread_id)

    async def delete(self, msg_id):
        await self._core.delete_msg(self.chat_id, msg_id)

    # ── Ask ────────────────────────────────────────────────────────
    async def ask_buttons(self, text, options, timeout=300, announce=None, cancel=True):
        return await self._core.ask_buttons(
            self.chat_id,
            self.thread_id,
            text,
            options,
            timeout=timeout,
            announce=announce,
            cancel=cancel,
        )

    async def menu(self, title, options):
        """Show a single-choice menu; return the chosen value (None if backed out).
        Nested automatically: opened from a menu tap, it renders as a submenu with
        Back to the parent. Use for pickers; `ask_buttons` stays for yes/no confirms."""
        return await self._core.menu(self.chat_id, self.thread_id, title, options)

    async def ask_text(self, prompt=None, timeout=180):
        return await self._core.ask_text(self.chat_id, self.thread_id, prompt, timeout=timeout)

    def await_next(self, handler):
        self._core.await_next(self.thread_id, handler)

    # ── Windows + cross-plugin ─────────────────────────────────────
    async def open(self, topic_class, name=None):
        """Create a window of `topic_class` (name deduped by the core) and return
        its live instance."""
        return await self._core.open_window(self.chat_id, topic_class, name)

    async def call_service(self, name, *args):
        """Invoke another plugin's @service for the current thread."""
        return await self._core.call_service(name, self.chat_id, self.thread_id, *args)

    def has_service(self, name) -> bool:
        return self._core.has_service(name)

    async def offer_handoff(self, message_id, payload, service, label):
        await self._core.offer_handoff(
            self.chat_id, self.thread_id, message_id, payload, service, label
        )


@launch("/new", "open a plain window")
class _CoreTopic(Topic):
    """The desktop window: no plain-text handler, no class commands. A released
    window drops to this class, keeping its history."""

    id = "core"
    icon = "🪟"
    menu_label = "Window"

    async def on_open(self) -> None:
        await self.send("🪟 plain window — run any command here, or / for the menu")


class _CorePlugin(Plugin):
    """The core's built-in commands as an ordinary plugin. Privileged: it holds a
    kernel reference (third-party plugins never do) for help/usage/close ops."""

    id = "core"
    topics = [_CoreTopic]

    def __init__(self, kernel):
        self._kernel = kernel

    @universal(
        "/close",
        "close this window (keep history or delete it)",
        icon="🗂",
        menu="window",
    )
    async def close(self, ctx):
        if ctx.topic is None:
            await ctx.send("run /close inside a topic")
            return
        choice, _ = await ctx.ask_buttons(
            "Close this window?",
            [("Keep (history stays)", "keep"), ("Delete", "delete")],
            timeout=60,
            announce=lambda label: f"🗂 {label}",
        )
        if choice == "keep":
            await ctx.topic.release()
        elif choice == "delete":
            await ctx.topic.close()

    @universal("/", "open the menu", menu="none", aliases=("/menu",))
    async def menu(self, ctx):
        await self._kernel.show_menu(ctx.chat_id, ctx.thread_id)

    @universal("/help", "what you can do here", menu="none", inline=True, aliases=("/?",))
    async def help(self, ctx):
        return self._kernel.help_text(ctx.thread_id)

    @universal("/usage", "show usage counts", icon="📊", inline=True)
    async def usage(self, ctx):
        return self._kernel.usage_text()

    @universal("/init", "bind this bot to you (mention in General)", menu="none")
    async def init(self, ctx):
        await ctx.send("already initialized ✓")

    @universal("/restart", "restart the bot service", icon="♻️", menu="window")
    async def restart(self, ctx):
        from tgforge.base.service import detached_restart

        service = self._kernel.config.service
        if not service:
            await ctx.send("no service configured (set `service` in bot.json)")
            return
        choice, _ = await ctx.ask_buttons(
            "Restart the bot service? In-flight turns finish first (drain).",
            [("♻️ Restart", "yes")],
            timeout=60,
        )
        if choice != "yes":
            await ctx.send("cancelled")
            return
        await ctx.send("restarting… back shortly (🟢 online here + in General when up)")
        detached_restart(service, self._kernel.config.home_path, announce_thread=ctx.thread_id)


class Kernel(Transport):
    def __init__(self, bot: AioBot, config: BotConfig, plugins: list | None = None):
        super().__init__(bot)
        self.config = config
        self.owner_id: int | None = config.owner_id
        self.chat_id: int | None = config.chat_id
        self.bot_username = config.bot_username
        self.shutting_down = False  # set on a graceful stop so drivers stay quiet
        self._start_mono = time.monotonic()
        self.db = AppDB(config.db_file)
        self.saved = self.db.saved(core_ns())
        # inline-button prompts awaiting a tap, keyed by prompt id
        self.pending_callbacks: dict[str, asyncio.Future] = {}
        # menu navigation stack per thread (the UINavigationController model): a list
        # of screen titles = the depth; the message every level edits; a push counter
        # so a menu can tell whether a tapped command navigated or just acted
        self._nav: dict[int, list[str]] = {}
        self._menu_msg: dict[int, int | None] = {}
        self._nav_pushes: dict[int, int] = {}
        self._menu_owner: dict[int, object] = {}  # token of the live menu session per thread
        self._active_prompt: dict[int, str] = {}  # pid of the tap a thread is awaiting now
        # a thread awaiting the owner's next plain message (ask_text / await_next)
        self._await_input: dict[int, Fallback] = {}
        # pending handoff taps
        self._handoffs: dict[str, tuple] = {}
        # ownership + live instances
        self.owners: dict[int, str] = {}  # thread_id -> class id
        self.instances: dict[int, Topic] = {}  # thread_id -> live Topic
        # plugins (core plugin first so its universals always exist)
        self._core_plugin = _CorePlugin(self)
        self.plugins: list = [self._core_plugin, *(plugins or [])]
        self.registry = build_registry(self.plugins)
        self.plugin_by_id = {p.plugin_id: p for p in self.plugins}
        self.plugin_saved = {}
        for p in self.plugins:
            ns = core_ns() if p.plugin_id == "core" else plugin_ns(p.plugin_id)
            p.saved = self.db.saved(ns)
            self.plugin_saved[p.plugin_id] = p.saved
        self._reconcile_task: asyncio.Task | None = None

    # ── Name registry (persisted in the core store) ────────────────

    def _names(self) -> dict[int, str]:
        return {int(k): v for k, v in self.saved.get("names", {}).items()}

    def _save_names(self, names: dict[int, str]) -> None:
        self.saved["names"] = {str(k): v for k, v in names.items()}

    def _save_owners(self) -> None:
        self.saved["owners"] = {str(k): v for k, v in self.owners.items()}

    def _dedup_name(self, name: str) -> str:
        existing = set(self._names().values())
        if name not in existing:
            return name
        k = 2
        while f"{name} #{k}" in existing:
            k += 1
        return f"{name} #{k}"

    def owner_of(self, thread_id: int | None) -> str | None:
        return self.owners.get(thread_id) if thread_id is not None else None

    # ── Topic + window ops ─────────────────────────────────────────

    async def create_topic(self, name: str) -> tuple[int, str] | None:
        """Create a forum topic (deduped name). Returns (thread_id, actual)."""
        actual = self._dedup_name(name)
        t = await self._call(lambda: self.bot.create_forum_topic(chat_id=self.chat_id, name=actual))
        if not t:
            return None
        names = self._names()
        names[t.message_thread_id] = actual
        self._save_names(names)
        return t.message_thread_id, actual

    def _instantiate(self, cls: type[Topic], thread_id: int, name: str) -> Topic:
        saved = self.db.saved(window_ns(cls.id, thread_id))
        inst = cls(self, thread_id, name, saved)
        entry = self.registry.classes.get(cls.id)
        inst.plugin = self.plugin_by_id.get(entry.plugin_id) if entry else None
        self.instances[thread_id] = inst
        return inst

    def _window_title(self, cls: type[Topic], name: str | None) -> str:
        """A topic's display name: the class icon + a capitalized base (the given
        name, else the class's menu label or its id title-cased)."""
        base = name or cls.menu_label or cls.id.title()
        if cls.icon and not base.startswith(cls.icon):
            base = f"{cls.icon} {base}"
        return base

    async def open_window(self, chat_id, topic_class: type[Topic], name=None) -> Topic | None:
        created = await self.create_topic(self._window_title(topic_class, name))
        if created is None:
            await self.send(chat_id, "couldn't create topic", None)
            return None
        tid, actual = created
        self.owners[tid] = topic_class.id
        self._save_owners()
        inst = self._instantiate(topic_class, tid, actual)
        base = actual
        if topic_class.icon and base.startswith(topic_class.icon):
            base = base[len(topic_class.icon) :].strip()
        inst.saved["base_name"] = base  # the deduped base ("Shell #2") title refresh keeps
        await inst.on_open()
        return inst

    async def adopt(self, thread_id: int, topic_class: type[Topic]) -> Topic:
        """Transform a window (a core window) into `topic_class`, keeping thread +
        name + history. The inverse of release()."""
        old = self.instances.get(thread_id)
        if old is not None:
            await self._teardown(old)
            self.db.drop(window_ns(self.owners.get(thread_id, "core"), thread_id))
        name = self._names().get(thread_id, topic_class.id)
        self.owners[thread_id] = topic_class.id
        self._save_owners()
        inst = self._instantiate(topic_class, thread_id, name)
        await inst.on_open()
        return inst

    async def _teardown(self, inst) -> None:
        """Close a window instance and reap every child it spawned — the one teardown
        path, so no close/release/transform can leak a subprocess."""
        await inst.on_close()
        await inst.reap_all()

    async def release_window(self, thread_id: int) -> None:
        inst = self.instances.get(thread_id)
        cls_id = self.owners.get(thread_id, "core")
        if inst is not None:
            await self._teardown(inst)
        self.db.drop(window_ns(cls_id, thread_id))
        self.owners[thread_id] = "core"
        self._save_owners()
        name = self._names().get(thread_id, "window")
        self._instantiate(_CoreTopic, thread_id, name)

    async def close_window(self, thread_id: int) -> None:
        inst = self.instances.get(thread_id)
        cls_id = self.owners.get(thread_id, "core")
        if inst is not None:
            await self._teardown(inst)
        deleted = await self._call(
            lambda: self.bot.delete_forum_topic(chat_id=self.chat_id, message_thread_id=thread_id)
        )
        if deleted is None:
            # the topic is still in Telegram — keep it owned so the reconcile sweep retries
            # rather than forgetting it into a permanently orphaned, unowned window.
            LOGGER.warning("delete_forum_topic failed for %s; left for reconcile", thread_id)
            return
        self.db.drop(window_ns(cls_id, thread_id))
        self._forget(thread_id)

    def _forget(self, thread_id: int) -> None:
        self.instances.pop(thread_id, None)
        self.owners.pop(thread_id, None)
        names = self._names()
        names.pop(thread_id, None)
        self._save_names(names)
        self._save_owners()

    def _compose_title(self, base: str, suffix: str | None) -> str:
        """`<base> · <suffix>` (the icon is added by rename_topic); the bare base when
        there is no suffix. `base` is the window's deduped name, not the class label, so
        a "#2" survives a title refresh."""
        return f"{base} · {suffix}" if suffix else base

    async def refresh_title(self, thread_id: int) -> None:
        """Rebuild a window's title from its live `title_suffix()`, keeping its deduped
        base name."""
        inst = self.instances.get(thread_id)
        if inst is not None:
            base = inst.saved.get("base_name") or (type(inst).menu_label or type(inst).id.title())
            await self.rename_topic(thread_id, self._compose_title(base, inst.title_suffix()))

    async def rename_topic(self, thread_id: int, name: str) -> bool:
        inst = self.instances.get(thread_id)
        if inst and inst.icon and not name.startswith(inst.icon):
            name = f"{inst.icon} {name}"  # a renamed topic keeps its class icon
        if self._names().get(thread_id) == name:
            return True  # unchanged → skip the API call (frequent refreshes stay flood-safe)
        ok = await self._call(
            lambda: self.bot.edit_forum_topic(
                chat_id=self.chat_id, message_thread_id=thread_id, name=name
            )
        )
        if ok is not None:
            names = self._names()
            names[thread_id] = name
            self._save_names(names)
            if inst is not None:
                inst.name = name  # keep the live instance's name in sync with the title
        return ok is not None

    # ── Services + handoff ─────────────────────────────────────────

    async def call_service(self, name: str, chat_id, thread_id, *args) -> None:
        """Invoke a registered @service as handler(ctx, *args) with a context for
        the target thread."""
        entry = self.registry.services.get(name)
        if entry is None:
            return
        pid, attr = entry
        plugin = self.plugin_by_id[pid]
        instance = self.instances.get(thread_id) if thread_id is not None else None
        ctx = self._ctx(pid, None, chat_id, thread_id, "", "", instance)
        await getattr(plugin, attr)(ctx, *args)

    def has_service(self, name: str) -> bool:
        return name in self.registry.services

    async def offer_handoff(self, chat_id, thread_id, message_id, payload, service, label):
        token = uuid.uuid4().hex[:8]
        self._handoffs[token] = (service, chat_id, thread_id, payload)
        kb = ui.stacked([ui.cta(label, "handoff", token)])
        ok = await self._call(
            lambda: self.bot.edit_message_reply_markup(
                chat_id=chat_id, message_id=message_id, reply_markup=kb
            )
        )
        if ok is None:
            self._handoffs.pop(token, None)

    async def _on_handoff(self, arg: str) -> None:
        info = self._handoffs.pop(arg, None)
        if info is not None:
            service, chat_id, thread_id, payload = info
            await self.call_service(service, chat_id, thread_id, payload)

    def uptime(self) -> float:
        return time.monotonic() - self._start_mono

    # ── Interaction helpers ────────────────────────────────────────

    async def ask_buttons(
        self, chat_id, thread_id, text, options, timeout=300, announce=None, cancel=True
    ):
        """Ask a one-tap choice. A Cancel option is appended by default — pass
        `cancel=False` to omit it, or a string to relabel it; tapping it (or a
        timeout) returns None. `announce(label) -> str` rewrites the prompt in place
        to reflect the pick (buttons drop); without it the buttons are just removed."""
        opts = list(options)
        if cancel:
            opts.append((cancel if isinstance(cancel, str) else "Cancel", CANCEL))
        pid = uuid.uuid4().hex[:8]
        kb = ui.stacked_choices([label for label, _v in opts], pid)
        kw = {"message_thread_id": thread_id} if thread_id is not None else {}
        msg = await self._call(lambda: self.bot.send_message(chat_id, text, reply_markup=kb, **kw))
        if msg is None:
            return None, None  # the prompt never rendered — don't hang for the timeout
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        self.pending_callbacks[pid] = fut
        try:
            idx = await asyncio.wait_for(fut, timeout)
        except TimeoutError:
            idx = None
        finally:
            self.pending_callbacks.pop(pid, None)
        if msg:
            await self._settle_prompt(chat_id, msg.message_id, opts, idx, announce)
        value = opts[idx][1] if idx is not None else None
        return (None if value == CANCEL else value), (msg.message_id if msg else None)

    async def _settle_prompt(self, chat_id, message_id, options, idx, announce) -> None:
        if announce is not None:
            text = announce(options[idx][0]) if idx is not None else "⏱ timed out"
            await self._call(
                lambda: self.bot.edit_message_text(
                    chat_id=chat_id, message_id=message_id, text=text
                )
            )
        else:
            await self._call(
                lambda: self.bot.edit_message_reply_markup(chat_id=chat_id, message_id=message_id)
            )

    async def edit_text(self, chat_id, message_id, text) -> None:
        """Rewrite a prior message in place (buttons drop) — used to announce the
        result of a tapped menu once its action has run."""
        if message_id is None:
            return
        await self._call(
            lambda: self.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text)
        )

    async def ask_text(self, chat_id, thread_id, prompt=None, timeout=180):
        """Await a typed reply. A prompt carries a ✖ Cancel button; tapping it (or a
        timeout) resolves None. The typed feeder and the tap share one future — a str
        result is the text, an int result is the Cancel tap."""
        fut: asyncio.Future = asyncio.get_event_loop().create_future()

        async def _feed(text):
            if not fut.done():
                fut.set_result(text)

        key = thread_id or 0
        pid = uuid.uuid4().hex[:8]
        self._await_input[key] = _feed
        msg_id = None
        if prompt:
            kb = ui.stacked_choices(["✖ Cancel"], pid)
            kw = {"message_thread_id": thread_id} if thread_id is not None else {}
            msg = await self._call(
                lambda: self.bot.send_message(chat_id, prompt, reply_markup=kb, **kw)
            )
            if msg is None:
                self._await_input.pop(key, None)
                return None  # the prompt never rendered — don't hang for the timeout
            msg_id = msg.message_id
            self.pending_callbacks[pid] = fut
        try:
            result = await asyncio.wait_for(fut, timeout)
        except TimeoutError:
            result = None
        finally:
            if self._await_input.get(key) is _feed:
                del self._await_input[key]
            self.pending_callbacks.pop(pid, None)
            await self._strip_markup(chat_id, msg_id)
        return None if isinstance(result, int) else result

    def await_next(self, thread_id: int | None, handler: Fallback) -> None:
        self._await_input[thread_id or 0] = handler

    # ── Help + usage text ──────────────────────────────────────────

    def help_text(self, thread_id: int | None) -> str:
        cls_id = self.owner_of(thread_id)
        lines: list[str] = ["Tap / (or /menu) for the button menu. Or type:"]
        if cls_id and cls_id in self.registry.classes and cls_id != "core":
            entry = self.registry.classes[cls_id]
            if entry.commands:
                lines.append(f"commands in this {cls_id} window:")
                for name, (_attr, help, _icon, _inline) in sorted(entry.commands.items()):
                    lines.append(f"  {name} — {help}")
        lines.append("commands anywhere:")
        for name, (_pid, _attr, help, _icon, _menu, _inline) in sorted(
            self.registry.universals.items()
        ):
            lines.append(f"  {name} — {help}")
        if not cls_id or cls_id == "core":
            lines.append("open a window:")
            for name, (_cid, help) in sorted(self.registry.launches.items()):
                lines.append(f"  {name} — {help}")
        return "\n".join(lines)

    def _menu_header(self, thread_id: int | None) -> str:
        """The menu's title — the window's own icon+name in a topic, else a neutral
        desktop title. Never a lone emoji (no jumbo render)."""
        cls_id = self.owner_of(thread_id)
        if cls_id and cls_id != "core" and cls_id in self.registry.classes:
            return self.registry.classes[cls_id].cls.menu_entry()
        return ui.DESKTOP_MENU_TITLE

    def _allowed(self, instance, kind: str, name: str) -> bool:
        """A window's own say over which commands run in it (default: all)."""
        return instance is None or instance.command_allowed(kind, name)

    def _menu_options(self, thread_id: int | None) -> list[tuple[str, str]]:
        """The buttons for this context: the current window's own commands, or the
        launchers on the desktop, plus the universals allowed here — each with its
        icon (a fallback if it declared none). The window filters its own set via
        `command_allowed`. The kernel curates nothing."""
        cls_id = self.owner_of(thread_id)
        instance = self.instances.get(thread_id) if thread_id is not None else None
        in_window = thread_id is not None
        opts: list[tuple[str, str]] = []
        if cls_id and cls_id != "core":  # an app window → its own commands
            entry = self.registry.classes[cls_id]
            opts += [
                (ui.menu_glyph(icon, name), f"ccmd:{name}")
                for name, (_attr, _help, icon, _inline) in sorted(entry.commands.items())
                if self._allowed(instance, "class", name)
            ]
        else:  # the desktop → launchers open/transform windows (primary names only)
            opts += [
                (self.registry.launch_class[name].menu_entry(), f"open:{name}")
                for name in sorted(self.registry.launches)
            ]
        for name, (_pid, _attr, _help, icon, menu, _inline) in sorted(
            self.registry.universals.items()
        ):
            if menu == "none" or (menu == "window" and not in_window):
                continue
            if not self._allowed(instance, "universal", name):
                continue
            opts.append((ui.menu_glyph(icon, name), f"cmd:{name}"))
        return opts + [("📋 Help", "cmd:/help")]

    async def _prompt_tap(self, chat_id, thread_id, text, options, msg_id):
        """Render a one-tap keyboard — a fresh message, or in place on `msg_id` —
        and await the tap. Returns (value, msg_id); the markup is left for the
        caller to re-render (a menu loops, a leaf swaps to Back)."""
        pid = uuid.uuid4().hex[:8]
        solo_back = bool(options) and options[-1][1] == MENU_BACK
        kb = ui.stacked_choices([label for label, _v in options], pid, solo_last=solo_back)
        if msg_id is None:
            kw = {"message_thread_id": thread_id} if thread_id is not None else {}
            msg = await self._call(
                lambda: self.bot.send_message(chat_id, text, reply_markup=kb, **kw)
            )
            if msg is None:
                return None, None  # the menu never rendered — don't hang for the timeout
            msg_id = msg.message_id
        else:
            await self._call(
                lambda: self.bot.edit_message_text(
                    chat_id=chat_id, message_id=msg_id, text=text, reply_markup=kb
                )
            )
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        self.pending_callbacks[pid] = fut
        key = thread_id or 0
        self._active_prompt[key] = pid  # the tap this thread awaits — supersede can dismiss it
        try:
            idx = await asyncio.wait_for(fut, 120)
        except TimeoutError:
            idx = None
        finally:
            self.pending_callbacks.pop(pid, None)
            if self._active_prompt.get(key) == pid:
                del self._active_prompt[key]
        return (options[idx][1] if idx is not None else None), msg_id

    def _is_inline(self, choice: str, thread_id: int | None) -> bool:
        """Whether a tapped entry is inline-able (returns text → render in place with
        Back) vs. one-off (mark triggered, it does its own output). Read from each
        command's own declaration — no list. `open:` (a launcher) is never inline."""
        kind, _, val = choice.partition(":")
        if kind == "cmd":
            entry = self.registry.universals.get(val)
            return bool(entry and entry[5])
        if kind == "ccmd":
            cls_id = self.owner_of(thread_id)
            entry = self.registry.classes.get(cls_id) if cls_id else None
            return bool(entry and val in entry.commands and entry.commands[val][3])
        return False

    def _supersede_prompt(self, key: int) -> None:
        """Dismiss the tap a thread is currently awaiting (resolve it to None), so a
        second menu opened concurrently doesn't share the first's live nav state."""
        pid = self._active_prompt.get(key)
        fut = self.pending_callbacks.get(pid) if pid else None
        if fut is not None and not fut.done():
            fut.set_result(None)

    @contextlib.asynccontextmanager
    async def _menu_frame(self, chat_id, thread_id, title):
        """Push one screen onto this thread's navigation stack for the duration; pop on
        exit. A fresh session (a true root, or a concurrent one opened while another is
        awaiting a tap) supersedes any live prompt and gets its own list + owner token, so
        the superseded session cleans up only its own state, never the newcomer's."""
        key = thread_id or 0
        fresh = self._active_prompt.get(key) is not None or not self._nav.get(key)
        if fresh:
            self._supersede_prompt(key)
            self._nav[key] = []  # a new list; a superseded session keeps its own reference
            self._menu_msg[key] = None
            token = object()
            self._menu_owner[key] = token
        else:
            token = self._menu_owner.get(key)
        stack = self._nav[key]
        stack.append(title)
        self._nav_pushes[key] = self._nav_pushes.get(key, 0) + 1
        try:
            yield
        finally:
            stack.pop()
            if not stack and self._menu_owner.get(key) is token:
                await self._strip_markup(chat_id, self._menu_msg.get(key))
                self._menu_msg.pop(key, None)
                self._nav.pop(key, None)
                self._nav_pushes.pop(key, None)
                self._menu_owner.pop(key, None)

    async def _screen(self, chat_id, thread_id, title, options) -> object:
        """Render the top screen on this thread's shared menu message (fresh the first
        time), adding a Back button when the stack is deeper than its root. Returns
        the chosen option value, MENU_BACK, or None (dismiss)."""
        key = thread_id or 0
        opts = list(options)
        if len(self._nav.get(key, [])) > 1:
            opts.append(("⬅ Back", MENU_BACK))
        choice, msg_id = await self._prompt_tap(
            chat_id, thread_id, title, opts, self._menu_msg.get(key)
        )
        self._menu_msg[key] = msg_id
        return choice

    async def menu(self, chat_id, thread_id, title, options) -> object:
        """Show a single-choice menu; return the chosen value (None if backed out or
        dismissed). Nested automatically: opened while a menu is already live on this
        thread, it renders as a submenu with Back to the parent (the nav-stack model —
        a command that opens a menu becomes a submenu)."""
        async with self._menu_frame(chat_id, thread_id, title):
            choice = await self._screen(chat_id, thread_id, title, options)
            return None if choice == MENU_BACK else choice

    async def show_menu(self, chat_id: int, thread_id: int | None) -> None:
        """Drive the context menu as the root of the navigation stack. A tapped command
        that opens its own menu (via ctx.menu) pushes a child screen — Back returns
        here. An inline command shows its output as a child screen with Back. A plain
        one-off marks this screen triggered and dismisses the stack."""
        key = thread_id or 0
        async with self._menu_frame(chat_id, thread_id, "menu"):
            while True:
                options = self._menu_options(thread_id)
                label_of = {value: label for label, value in options}
                choice = await self._screen(
                    chat_id, thread_id, self._menu_header(thread_id), options
                )
                if choice is None or choice == MENU_BACK:
                    return  # dismissed, or backed out of the root
                mid = self._menu_msg[key]
                kind, _, val = choice.partition(":")
                if kind == "open":  # a launcher takes over → mark triggered, dismiss
                    await self.edit_text(chat_id, mid, ui.triggered(label_of[choice]))
                    await self._route_launch(chat_id, thread_id, val, "", self.owner_of(thread_id))
                    return
                if self._is_inline(choice, thread_id):
                    body = await self._run_choice(chat_id, thread_id, kind, val)
                    text = body if isinstance(body, str) and body.strip() else "done"
                    async with self._menu_frame(chat_id, thread_id, "result"):
                        back = await self._screen(chat_id, thread_id, text, [])
                    if back is None:  # dismissed on the result screen
                        return
                    continue  # Back → re-render the menu
                # a command: it may open its own submenu(s) via ctx.menu. If it pushed a
                # screen we stay here (Back from the submenu landed back on us); if it
                # only acted, mark this screen triggered and dismiss.
                before = self._nav_pushes.get(key, 0)
                await self._run_choice(chat_id, thread_id, kind, val)
                if self._nav_pushes.get(key, 0) > before:
                    continue
                await self.edit_text(chat_id, mid, ui.triggered(label_of[choice]))
                return

    async def _strip_markup(self, chat_id: int, msg_id: int | None) -> None:
        if msg_id is not None:
            await self._call(
                lambda: self.bot.edit_message_reply_markup(chat_id=chat_id, message_id=msg_id)
            )

    async def _run_choice(self, chat_id, thread_id, kind, val):
        """Run a tapped menu entry; return the handler's result (a str body for an
        inline command, else None)."""
        if kind == "cmd":
            return await self._run_universal(chat_id, thread_id, val)
        if kind == "ccmd":
            return await self._run_class_command(chat_id, thread_id, val)
        return None

    async def _run_universal(self, chat_id: int, thread_id: int | None, name: str):
        entry = self.registry.universals.get(name)
        if entry is None:
            return None
        pid, attr, _help, _icon, _menu, _inline = entry
        instance = self.instances.get(thread_id) if thread_id is not None else None
        if not self._allowed(instance, "universal", name):
            return None
        ctx = self._ctx(pid, None, chat_id, thread_id, name, "", instance)
        self._bump(pid, name)
        return await getattr(self.plugin_by_id[pid], attr)(ctx)

    async def _run_class_command(self, chat_id: int, thread_id: int | None, name: str):
        instance = self.instances.get(thread_id) if thread_id is not None else None
        cls_id = self.owner_of(thread_id)
        entry = self.registry.classes.get(cls_id) if cls_id else None
        if instance is None or entry is None or name not in entry.commands:
            return None
        if not self._allowed(instance, "class", name):
            return None
        attr, _help, _icon, _inline = entry.commands[name]
        ctx = self._ctx(entry.plugin_id, None, chat_id, thread_id, name, "", instance)
        self._bump(cls_id, name)
        return await getattr(instance, attr)(ctx)

    def usage_text(self) -> str:
        totals = self.db.usage_totals()
        if not totals:
            return "no usage recorded yet"
        lines = ["usage (all-time counts):"]
        for scope, name, count in totals[:40]:
            lines.append(f"  {scope} · {name}: {count}")
        return "\n".join(lines)

    def _bump(self, scope: str, name: str) -> None:
        day = datetime.date.today().isoformat()
        self.db.bump(day, scope, name)

    # ── Lifecycle ──────────────────────────────────────────────────

    async def startup(self) -> None:
        me = await self.bot.get_me()
        self.bot_username = me.username
        self.config.bot_username = me.username
        self.config.save()
        # rebuild live windows from persisted ownership
        self.owners = {int(k): v for k, v in self.saved.get("owners", {}).items()}
        names = self._names()
        for tid, cls_id in list(self.owners.items()):
            cls = self._class_by_id(cls_id)
            if cls is None:
                continue
            inst = self._instantiate(cls, tid, names.get(tid, cls_id))
            try:
                await inst.on_revive()
            except Exception:
                LOGGER.exception("on_revive failed for %s (%s)", tid, cls_id)
        await self._announce_online()
        self._reconcile_task = asyncio.create_task(self._reconcile_loop())

    async def _announce_online(self) -> None:
        """A readiness ping in General on every startup (the lobby heartbeat), plus a
        ping in the topic that asked for the restart, if one recorded itself."""
        if self.chat_id is None:
            return
        await self.send(self.chat_id, f"🟢 @{self.bot_username} online", None)
        from tgforge.base.service import pop_restart_announce

        tid = pop_restart_announce(self.config.home_path)
        if tid is not None:
            await self.send(self.chat_id, "🟢 back online after a restart", tid)
            inst = self.instances.get(tid)
            if inst is not None:
                try:
                    await inst.on_restarted()
                except Exception:
                    LOGGER.exception("on_restarted failed for %s", tid)

    async def broadcast_shutdown(self) -> None:
        """A graceful stop: flag it (drivers skip crash warnings) and let every live
        window settle its UI before the process exits."""
        self.shutting_down = True
        for inst in list(self.instances.values()):
            try:
                await inst.on_shutdown()
            except Exception:
                LOGGER.exception("on_shutdown failed (%s)", inst.thread_id)

    def _class_by_id(self, cls_id: str) -> type[Topic] | None:
        entry = self.registry.classes.get(cls_id)
        return entry.cls if entry else None

    # ── Reconcile sweep (manual-deletion cleanup) ──────────────────

    async def _reconcile_loop(self) -> None:
        while True:
            await asyncio.sleep(RECONCILE_INTERVAL)
            for tid in list(self.owners):
                if not await self._thread_alive(tid):
                    await self._drop_deleted(tid)

    async def _thread_alive(self, thread_id: int) -> bool:
        """Probe a thread invisibly by re-setting its own current title (a no-op edit, so
        no "typing…" flicker every sweep); only an explicit 'thread not found' means gone.
        A transient error is not treated as deletion. Untitled → the visible typing probe."""
        name = self._names().get(thread_id)
        try:
            if name:
                await self.bot.edit_forum_topic(
                    chat_id=self.chat_id, message_thread_id=thread_id, name=name
                )
            else:
                await self.bot.send_chat_action(
                    chat_id=self.chat_id, action="typing", message_thread_id=thread_id
                )
            return True
        except TelegramAPIError as e:
            return "not found" not in str(e).lower()

    async def _drop_deleted(self, thread_id: int) -> None:
        inst = self.instances.get(thread_id)
        if inst is not None:
            try:
                await self._teardown(inst)
            except Exception:
                LOGGER.exception("on_close failed on delete (%s)", thread_id)
        self.db.drop(window_ns(self.owners.get(thread_id, "core"), thread_id))
        self._forget(thread_id)

    # ── Routing ────────────────────────────────────────────────────

    def _ctx(self, plugin_id, message, chat_id, thread_id, text, args, instance):
        from_user = getattr(message, "from_user", None) if message else None
        user_id = from_user.id if from_user else None
        return Context(
            self,
            chat_id,
            thread_id,
            message,
            text,
            args,
            instance,
            self.plugin_saved.get(plugin_id),
            user_id,
        )

    async def _run(self, fn, ctx) -> bool:
        """Call a handler; True if it consumed the event (declined only on False)."""
        return (await fn(ctx)) is not False

    async def _run_command(self, fn, ctx, inline) -> bool:
        """Run a command handler. Declines on False (router falls through). An inline
        command returns its body text, which the router sends here (typed path)."""
        result = await fn(ctx)
        if result is False:
            return False
        if inline and isinstance(result, str):
            await self.send(ctx.chat_id, result, ctx.thread_id)
        return True

    async def handle_message(self, message: Message) -> None:
        text = (message.text or message.caption or "").strip()
        has_media = bool(message.photo or message.document)
        if not text and not has_media:
            return
        user_id = message.from_user.id if message.from_user else None
        if user_id is None:
            return
        chat_id = message.chat.id
        thread_id = message.message_thread_id
        mentioned = f"@{self.bot_username}" in text
        text = text.replace(f"@{self.bot_username}", "").strip()

        if self.owner_id is None:
            if mentioned and text.lower().startswith("/init"):
                self.owner_id = user_id
                self.chat_id = chat_id
                self.config.owner_id = user_id
                self.config.chat_id = chat_id
                self.config.save()
                await self.send(chat_id, f"@{self.bot_username} initialized", thread_id)
            return
        if chat_id != self.chat_id:
            return  # user authorization is enforced by the auth middleware

        # 1. a pending prompt (await_next / ask_text) wins first
        pending = self._await_input.get(thread_id or 0)
        if pending is not None:
            if has_media and not text:
                return  # a media-only message must not feed "" and cancel a text prompt
            del self._await_input[thread_id or 0]
            await pending(text)
            return

        in_general = thread_id is None
        if in_general and not mentioned:
            return  # General acts only on a mention

        instance = self.instances.get(thread_id) if thread_id is not None else None
        cls_id = self.owner_of(thread_id)

        if mentioned and not text and not has_media:
            await self.show_menu(chat_id, thread_id)  # a bare @mention → summon the menu
            return

        # a window may take slashes as raw input (a shell): route them as text, not
        # commands. A mentioned slash still runs as a command (an explicit escape).
        raw_slash = instance is not None and not instance.slash_commands and not mentioned
        if text.startswith("/") and not raw_slash:
            await self._route_slash(message, chat_id, thread_id, text, instance, cls_id)
        else:
            await self._route_text(message, chat_id, thread_id, text, instance, cls_id)

    def _split(self, text: str) -> tuple[str, str]:
        parts = text.split(maxsplit=1)
        cmd = parts[0].lower().split("@")[0]
        args = parts[1].strip() if len(parts) > 1 else ""
        return cmd, args

    async def _route_slash(self, message, chat_id, thread_id, text, instance, cls_id):
        cmd, args = self._split(text)
        # escape prefix: N leading slashes bypass N-1 matching handler tiers, so a
        # command a higher tier would eat can be forced to the next one (e.g. //usage
        # skips the bot universal → the window's catch → the agent). Extras are
        # stripped so a bypassed command reaches the agent with a single slash.
        bypass = max(0, (len(cmd) - len(cmd.lstrip("/"))) - 1)
        if bypass:
            cmd = "/" + cmd.lstrip("/")
            text = "/" + text.lstrip("/")

        def _skip() -> bool:
            nonlocal bypass
            if bypass > 0:
                bypass -= 1
                return True
            return False

        # class command (resolves aliases to the primary name)
        if cls_id and cls_id in self.registry.classes:
            entry = self.registry.classes[cls_id]
            cname = entry.resolve(cmd)
            if cname and self._allowed(instance, "class", cname) and not _skip():
                attr, _help, _icon, inline = entry.commands[cname]
                ctx = self._ctx(entry.plugin_id, message, chat_id, thread_id, text, args, instance)
                self._bump(cls_id, cname)
                if await self._run_command(getattr(instance, attr), ctx, inline):
                    return

        # universal command (core universals always; others need allow_universal / General)
        uname = self.registry.resolve_universal(cmd)
        if uname:
            pid, attr, _help, _icon, _menu, inline = self.registry.universals[uname]
            allowed = pid == "core" or instance is None or instance.allow_universal
            allowed = allowed and self._allowed(instance, "universal", uname)
            if allowed and not _skip():
                plugin = self.plugin_by_id[pid]
                ctx = self._ctx(pid, message, chat_id, thread_id, text, args, instance)
                self._bump(pid, uname)
                if await self._run_command(getattr(plugin, attr), ctx, inline):
                    return

        # launch command (new window / transform / ignored, by context)
        lname = self.registry.resolve_launch(cmd)
        if lname and not _skip():
            if await self._route_launch(chat_id, thread_id, lname, args, cls_id):
                return

        # on_message (sees slashes) then on_unknown, then the General-only fallback
        if await self._route_catch(message, chat_id, thread_id, text, instance, cls_id, args):
            return
        await self._unhandled(chat_id, thread_id)

    async def _route_launch(self, chat_id, thread_id, cmd, args, cls_id) -> bool:
        cls = self.registry.launch_class[cmd]
        self._bump("core", cmd)
        if thread_id is None:  # General → always a new window
            await self.open_window(chat_id, cls, args or None)
            return True
        if cls_id == "core":  # a core window → new or transform (core class: always new)
            if cls.id == "core":
                await self.open_window(chat_id, cls, args or None)
                return True
            choice, _ = await self.ask_buttons(
                chat_id,
                thread_id,
                f"Open {cls.id}:",
                [("New window", "new"), ("Transform this window", "transform")],
                timeout=60,
                announce=lambda label: f"🪄 {label}",
            )
            if choice == "new":
                await self.open_window(chat_id, cls, args or None)
            elif choice == "transform":
                await self.adopt(thread_id, cls)
            return True
        return False  # any other window ignores the launch (falls through)

    async def _route_catch(self, message, chat_id, thread_id, text, instance, cls_id, args) -> bool:
        if cls_id and cls_id in self.registry.classes and instance is not None:
            entry = self.registry.classes[cls_id]
            for attr in (entry.on_message, entry.on_unknown):
                if attr is None:
                    continue
                ctx = self._ctx(entry.plugin_id, message, chat_id, thread_id, text, args, instance)
                if await self._run(getattr(instance, attr), ctx):
                    return True
        return False

    async def _route_text(self, message, chat_id, thread_id, text, instance, cls_id):
        # prefixes (universal, e.g. claude's ! / !!)
        for char, (pid, attr, _help) in self._sorted_prefixes():
            if text.startswith(char):
                plugin = self.plugin_by_id[pid]
                allowed = instance is None or instance.allow_universal
                if allowed:
                    ctx = self._ctx(pid, message, chat_id, thread_id, text, text, instance)
                    self._bump(pid, char)
                    if await self._run(getattr(plugin, attr), ctx):
                        return
        if await self._route_catch(message, chat_id, thread_id, text, instance, cls_id, text):
            return
        await self._unhandled(chat_id, thread_id)

    async def _unhandled(self, chat_id: int, thread_id: int | None) -> None:
        """Input nothing consumed → the help text (which points at `/` for the menu).
        A plain reply, never the interactive menu: a stray keystroke should not pop a
        keyboard, and going silent leaves the user stuck."""
        await self.send(chat_id, self.help_text(thread_id), thread_id)

    def _sorted_prefixes(self):
        # longer prefixes first so `!!` beats `!`
        return sorted(self.registry.prefixes.items(), key=lambda kv: -len(kv[0]))

    # ── Callbacks ──────────────────────────────────────────────────

    async def handle_callback(self, callback: CallbackQuery) -> None:
        data = callback.data or ""
        head = data.split(":")[0]
        if head in self.pending_callbacks:
            fut = self.pending_callbacks.get(head)
            if fut and not fut.done():
                fut.set_result(int(data.split(":")[1]))
        else:
            cta = ui.parse_cta(data)
            act = ui.parse_act(data)
            if cta is not None and cta[0] == "handoff":
                await self._on_handoff(cta[1])
            elif act is not None:
                await self._route_action(callback, act[0], act[1])
        await self._call(lambda: callback.answer())

    async def _route_action(self, callback, name, arg) -> None:
        msg = callback.message
        thread_id = msg.message_thread_id if msg else None
        instance = self.instances.get(thread_id) if thread_id is not None else None
        cls_id = self.owner_of(thread_id)
        if instance is None or not cls_id:
            return
        entry = self.registry.classes.get(cls_id)
        attr = entry.actions.get(name) if entry else None
        if attr is None:
            return
        chat_id = msg.chat.id if msg else self.chat_id
        ctx = self._ctx(entry.plugin_id, msg, chat_id, thread_id, "", arg, instance)
        tapper = getattr(callback, "from_user", None)
        if tapper is not None:
            ctx.user_id = tapper.id  # the tapping user, not the bot that authored the message
        await getattr(instance, attr)(ctx, arg)
