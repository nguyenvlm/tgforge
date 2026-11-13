"""The Kernel — the runtime plugins register into, an operating system for windows.

This module is the whole platform-neutral framework core: the process probes +
subprocess reaper, persistence (`AppDB`, one SQLite file; `SavedDict`, namespaced
dict-like views), the plugin declaration decorators, the `Topic` and `Plugin` bases,
the neutral event types (`Inbound`, `Tap`) and call outcomes an adapter reports, the
`Transport` (one platform adapter behind one pacer and flood-wait deadline, with the
rich-then-plain retry, chunking, and the frame-skip output for live messages and
titles), the command registry (built once at startup; aborts the launch on any name
clash), the per-call `Context` a handler receives, and the `Kernel` itself. The kernel
holds one `Transport` per configured platform and picks a window's by its key; it owns
the chain-of-responsibility router, the owner binding, window ownership and lifecycle
(open / adopt / release / close), the interaction helpers (button/text prompts),
cross-plugin services, daily usage counts, and the reconcile sweep that drops
manually-deleted windows. A plugin never receives the kernel — it
works through a per-call `Context` and its `Topic` instance. The built-in commands
(`/`, `/new`, `/close`, `/usage`, `/help`, `/init`, `/restart`) ride an implicit plugin
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
import signal
import sqlite3
import subprocess
import sys
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

from tgforge.base import ui
from tgforge.base.config import BotConfig
from tgforge.base.ui import MAX_MSG, chunks, md_chunks

LOGGER = logging.getLogger("tgforge")

_NOT_MODIFIED = object()  # a benign edit no-op: the new content already matches the message
_FLOODED = object()  # a call the platform flood-waited; the deadline is in `flood_until`
# a delete's outcome (`_attempt(delete_outcome=True)`), besides a landed True and _FLOODED
_GONE = object()  # "message to delete not found": the message is not there
_UNSURE = object()  # transient retries ran out: the message may still be there
_REFUSED = object()  # any other rejection: the message stays
STUB_TEXT = "·"  # what an undeletable bottom-frame copy is edited down to
FRAME_MEMORY = 256  # landed message frames kept for the no-op check (oldest forgotten)


def _first_page(pages: ui.Pages) -> ui.Pages:
    """`pages` cut to its first page: what a card's frame shows (rendered alone)."""
    return ui.Pages(pages.summary, pages.pages[:1], pages.note)


def _resolve(waiters: list, landed: bool) -> None:
    for waiter in waiters:
        if not waiter.done():
            waiter.set_result(landed)


def _cap(text: str) -> str:
    """Last-resort MAX_MSG guard before an adapter call. A hit means a caller sent an
    oversized body and the tail (an answer's final words) is being lost mid-word — that
    is a composition bug, so log it loudly instead of cutting silently."""
    if len(text) > MAX_MSG:
        LOGGER.warning(
            "message over MAX_MSG (%d), tail truncated — fix the caller's compose", len(text)
        )
        return text[:MAX_MSG]
    return text


REQUEST_TIMEOUT = 45  # s: per-request HTTP timeout, so a stuck socket can't wedge a call
STALL_WARN = 30  # s: log a warning when a live turn's reader has been quiet this long
# Telegram caps a group/supergroup at 20 messages/minute (all topics share one budget).
# Pace below that with margin so we self-limit before the 429 ever fires.
PACE_MAX_PER_MIN = 18  # sustained sends/edits per minute across the whole transport
PACE_BURST = 3  # bucket capacity: how many may fire back-to-back before pacing kicks in
TITLE_MIN_INTERVAL = 5.0  # s between renames of one topic (each posts a service message)
SPARE_SLOT_POLL = 0.1  # s: how often a frame push re-checks for a spare slot
SHUTDOWN_FLUSH = 10.0  # s: how long a graceful stop waits for pending frames to land
TRANSIENT_RETRIES = 3  # retries of a call hit by a `Transient` error (network or 5xx)
TRANSIENT_BACKOFF = 1.0  # s before the first such retry, doubling (1, 2, 4 s)
BOTTOM_RESEND_ATTEMPTS = 3  # failed (non-flood) re-sends of a bottom frame before it waits


class _Pacer:
    """A shared token bucket that paces every outbound call of one platform so we stay under
    the per-group rate. An essential call (`take`) waits for a slot; a frame push
    (`take_spare`) also waits until no essential call is pending, so a reply never
    loses its slot to a spinner tick. `per_min<=0` disables pacing entirely."""

    def __init__(self, per_min: float, burst: float):
        self.enabled = per_min > 0
        self.refill = per_min / 60.0  # tokens per second
        self.capacity = max(1.0, burst)
        self.tokens = self.capacity
        self.last = time.monotonic()
        self.waiting = 0  # essential calls not yet done (set by the transport)
        self._lock = asyncio.Lock()

    def _replenish(self):
        now = time.monotonic()
        self.tokens = min(self.capacity, self.tokens + (now - self.last) * self.refill)
        self.last = now

    async def take(self) -> None:
        if not self.enabled:
            return
        while True:
            async with self._lock:
                self._replenish()
                if self.tokens >= 1:
                    self.tokens -= 1
                    return
                delay = (1 - self.tokens) / self.refill
            await asyncio.sleep(delay)

    def give_back(self, count: int = 1) -> None:
        """Return slots taken for calls that did not go out."""
        if self.enabled:
            self.tokens = min(self.capacity, self.tokens + count)

    async def take_spare(self) -> None:
        if not self.enabled:
            return
        while True:
            async with self._lock:
                self._replenish()
                if not self.waiting and self.tokens >= 1:
                    self.tokens -= 1
                    return
            await asyncio.sleep(self.spare_delay(1))

    @property
    def fits_pair(self) -> bool:
        """Whether two slots can be held at once (pacing off, or a burst of 2 or more)."""
        return not self.enabled or self.capacity >= 2

    def take_spare_now(self) -> bool:
        """Take a spare slot if one is free right now; never waits."""
        if not self.enabled:
            return True
        self._replenish()
        if self.waiting or self.tokens < 1:
            return False
        self.tokens -= 1
        return True

    def spare_delay(self, count: int) -> float:
        """Seconds until `count` spare slots are free (at least SPARE_SLOT_POLL)."""
        if not self.enabled:
            return 0.0
        self._replenish()
        if self.waiting or self.tokens >= count:
            return SPARE_SLOT_POLL
        return max((count - self.tokens) / self.refill, SPARE_SLOT_POLL)


def setup_logging() -> None:
    """Attach a stderr handler to the `tgforge` logger once (level via TGFORGE_LOG_LEVEL,
    default INFO). Without this the logger has no handler and warnings vanish."""
    if LOGGER.handlers:
        return
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    LOGGER.addHandler(handler)
    LOGGER.setLevel(os.environ.get("TGFORGE_LOG_LEVEL", "INFO").upper())
    LOGGER.propagate = False


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
    closes and no child leaks. A child leading its own process group (spawned with
    `start_new_session=True`, a PTY shell) is killed with the whole group: a shell's
    children outlive a kill of the shell alone and hold its pipes open. Safe if it
    already exited (the child watcher may reap it first, racing a bare kill to
    ProcessLookupError). The one reaper for every spawn."""
    if proc.returncode is None:
        try:
            if os.getpgid(proc.pid) == proc.pid:
                os.killpg(proc.pid, signal.SIGKILL)
            else:
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


def file_holders(path: str) -> set[int] | None:
    """PIDs of the processes holding `path` open, or None where there is no probe
    (Linux only: a /proc fd scan)."""
    if not IS_LINUX:
        return None
    try:
        fd_dirs = list(Path("/proc").glob("[0-9]*/fd"))
    except OSError:
        return set()
    holders: set[int] = set()
    for fd_dir in fd_dirs:
        try:
            fds = list(fd_dir.iterdir())
        except OSError:
            continue  # the process exited mid-scan
        for fd in fds:
            try:
                if os.readlink(fd) == path:
                    holders.add(int(fd_dir.parent.name))
                    break
            except OSError:
                continue  # this fd closed mid-scan; keep checking the rest
    return holders


def _process_table() -> dict[int, tuple[int, int, int]]:
    """pid → (ppid, pgid, sid) for every live process, read from /proc."""
    table: dict[int, tuple[int, int, int]] = {}
    for stat in Path("/proc").glob("[0-9]*/stat"):
        try:
            fields = stat.read_text().rsplit(")", 1)[1].split()
            table[int(stat.parent.name)] = (int(fields[1]), int(fields[2]), int(fields[3]))
        except (OSError, IndexError, ValueError):
            continue  # the process exited mid-scan
    return table


def _job_tree(path: str) -> set[int] | None:
    """Every process of the job writing `path`: its holders, every process in a session
    whose leader is a holder, and all their descendants (a child that left the group or
    session via setpgid/setsid is still a descendant). None where there is no probe.
    Raises if the set would reach this process's own pid, group, or session."""
    holders = file_holders(path)
    if holders is None:
        return None
    table = _process_table()
    # a holder that leads its session (the job's shell) brings the whole session; a
    # reader in someone else's session (a `tail -f` in a terminal) brings only itself
    sessions = {pid for pid in holders if pid in table and table[pid][2] == pid}
    tree = holders | {pid for pid, (_, _, sid) in table.items() if sid in sessions}
    children: dict[int, list[int]] = {}
    for pid, (ppid, _, _) in table.items():
        children.setdefault(ppid, []).append(pid)
    stack = list(tree)
    while stack:
        for child in children.get(stack.pop(), []):
            if child not in tree:
                tree.add(child)
                stack.append(child)
    own = (os.getpid(), os.getpgrp(), os.getsid(0))
    hits = sorted(
        pid
        for pid in tree
        if pid == own[0] or (pid in table and (table[pid][1] == own[1] or table[pid][2] == own[2]))
    )
    if hits:
        raise RuntimeError(f"job tree of {path} reaches this process's own group: {hits}")
    return tree


async def kill_file_holders(path: str, grace: float = 2.0) -> int | None:
    """Stop the job writing `path` (see `_job_tree`): SIGTERM the whole tree, then
    SIGKILL whatever outlives `grace` seconds. Returns how many processes were
    signalled, or None where there is no probe."""
    import signal

    tree = _job_tree(path)
    if tree is None:
        return None
    for pid in tree:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        live = [pid for pid in tree if _alive(pid)]
        if not live:
            break
        await asyncio.sleep(0.1)
    for pid in tree:
        if _alive(pid):
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(pid, signal.SIGKILL)
    return len(tree)


def _alive(pid: int) -> bool:
    """True if `pid` exists and is not a zombie."""
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except (OSError, IndexError):
        return False
    return state != "Z"


def file_held_open(path: str) -> bool:
    """True if any process holds `path` open. On platforms with no probe, returns
    True — the conservative answer, since callers use a False to declare a process
    gone and a false negative would end a live job early."""
    if IS_LINUX:
        return bool(file_holders(path))
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


def window_ns(class_id: str, window: str) -> str:
    return f"win:{class_id}:{window}"


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
    """A class's plain-text catch handler (sees every event; `return False` declines)."""
    fn._tg_on_message = True
    return fn


def on_unknown(fn):
    """A class's handler for a slash nothing above consumed (`return False` declines)."""
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

    def __init__(self, core, window: str, name: str, saved: SavedDict):
        self._core = core
        self.window = window  # the window key (see `Inbound.window`)
        self.name = name
        self.saved = saved
        self.plugin = None  # the owning Plugin (set by the kernel); for its store/roots
        self._procs: set = set()  # every subprocess this window spawned; reaped on close
        self._timed_deletes: set[asyncio.Task] = set()  # pending send_temporary deletes

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

    async def refresh_title(self, immediate: bool = False) -> None:
        """Recompose the topic title from the current `title_suffix()` and return at
        once: the rename is a frame-skip title (see `Transport.show_title`), skipped
        when unchanged, so a burst of state changes gives one rename. `immediate` (an
        explicit rename) skips the spacing between renames."""
        self._core.refresh_title(self.window, immediate=immediate)

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
        await self._core.release_window(self.window)

    async def close(self) -> None:
        """Delete this window."""
        await self._core.close_window(self.window)

    async def rename(self, name: str) -> None:
        """An explicit rename: lands as soon as a slot is free (no rename spacing)."""
        self._core.rename_topic(self.window, name, immediate=True)
        self.name = name

    # ── Replies bound to this window ───────────────────────────────
    @property
    def _tx(self) -> Transport:
        return self._core.transport_for(self.window)

    @property
    def adapter(self):
        """This window's platform adapter, for its name, limits and size measures."""
        return self._tx.adapter

    async def download_file(self, attachment, dest):
        """Download an inbound attachment of this window to `dest` (None on failure)."""
        return await self._tx.download_file(attachment, dest)

    async def run_message_call(self, call):
        """Run `call`, a zero-arg factory of a transport call that posts or deletes a
        message in this window (the kernel's menus and prompts come here too). Default:
        at once; a window that orders its messages overrides it."""
        return await call()

    async def send(self, text, reply_to=None):
        tx, window = self._tx, self.window
        return await self.run_message_call(lambda: tx.send(window, text, reply_to=reply_to))

    async def send_temporary(self, text, lifetime: float):
        """Send a brief notice and delete it `lifetime` seconds later; the window's bottom
        frames stay where they are."""
        tx, window = self._tx, self.window
        msg_id = await self.run_message_call(lambda: tx.send(window, text, buries_bottom=False))
        if msg_id is not None:
            task = asyncio.get_running_loop().create_task(self._delete_after(msg_id, lifetime))
            self._timed_deletes.add(task)
            task.add_done_callback(self._timed_deletes.discard)
        return msg_id

    async def _delete_after(self, msg_id, lifetime: float) -> None:
        await asyncio.sleep(lifetime)
        await self.delete(msg_id)

    async def send_rich(self, text, reply_to=None, reply_markup=None):
        tx, window = self._tx, self.window
        return await self.run_message_call(
            lambda: tx.send_rich(window, text, reply_to=reply_to, reply_markup=reply_markup)
        )

    async def edit(self, msg_id, text, reply_markup=None):
        return await self._tx.edit(self.window, msg_id, text, reply_markup=reply_markup)

    async def edit_md(self, msg_id, md, plain, reply_markup=None):
        return await self._tx.edit_md(self.window, msg_id, md, plain, reply_markup=reply_markup)

    async def edit_rich(self, msg_id, text):
        return await self._tx.edit_rich(self.window, msg_id, text)

    async def send_md(
        self, md, plain=None, reply_to=None, silent=False, reply_markup=None, first_id=None
    ):
        """Send standard Markdown or `Pages` (`Transport.send_md`)."""
        tx, window = self._tx, self.window
        return await self.run_message_call(
            lambda: tx.send_md(
                window,
                md,
                plain,
                reply_to=reply_to,
                silent=silent,
                reply_markup=reply_markup,
                first_id=first_id,
            )
        )

    def show_frame(self, msg_id, plain, md=None, reply_markup=None) -> None:
        """Frame-skip update of a live message in this window (`Transport.show_frame`)."""
        self._tx.show_frame(self.window, msg_id, plain, md, reply_markup=reply_markup)

    def land_frame(self, msg_id, plain, md=None, reply_markup=None):
        """A finalizing edit of a message in this window (`Transport.land_frame`);
        returns a future of whether it landed."""
        return self._tx.land_frame(self.window, msg_id, plain, md, reply_markup=reply_markup)

    def show_bottom_frame(self, name: str, plain, md=None, reply_markup=None) -> None:
        """Keep this window's message `name` as its bottom-most message (see
        `Transport.show_bottom_frame`); `plain=None` deletes it."""
        self._tx.show_bottom_frame(
            self.window, f"{self.window}:{name}", plain, md, reply_markup=reply_markup
        )

    def bottom_frame_id(self, name: str):
        return self._tx.bottom_frame_id(f"{self.window}:{name}")

    async def set_markup(self, msg_id, reply_markup):
        """Attach/replace the buttons of an existing message (None: no buttons)."""
        await self._tx.set_buttons(self.window, msg_id, reply_markup)

    async def send_file(self, path):
        tx, window = self._tx, self.window
        return await self.run_message_call(lambda: tx.send_file(window, path))

    async def send_photo(self, media_type, b64, reply_markup=None):
        tx, window = self._tx, self.window
        return await self.run_message_call(
            lambda: tx.send_photo_b64(window, media_type, b64, reply_markup=reply_markup)
        )

    async def edit_photo(self, msg_id, media_type, b64, reply_markup=None):
        return await self._tx.edit_photo_b64(
            self.window, msg_id, media_type, b64, reply_markup=reply_markup
        )

    async def delete(self, msg_id) -> None:
        tx, window = self._tx, self.window
        await self.run_message_call(lambda: tx.delete_msg(window, msg_id))

    async def ask_buttons(self, text, options, timeout=300, announce=None, cancel=True):
        return await self._core.ask_buttons(
            self.window,
            text,
            options,
            timeout=timeout,
            announce=announce,
            cancel=cancel,
        )

    async def menu(self, title, options):
        """Single-choice menu; returns the chosen value (None if backed out). Nests as
        a submenu when opened from a menu tap. See `Context.menu`."""
        return await self._core.menu(self.window, title, options)

    async def ask_text(self, prompt=None, timeout=180):
        return await self._core.ask_text(self.window, prompt, timeout=timeout)

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
            choice, msg_id = await self._core._prompt_tap(self.window, header, opts, msg_id)
            if choice in (None, "cancel", "use"):
                await self._core._strip_markup(self.window, msg_id)
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


# ── Neutral events, call outcomes, and the platform adapter ────────
# An adapter (`base/telegram.py`, `base/slack.py`) is the only code that talks to a
# platform: it turns updates into `Inbound` / `Tap`, makes the calls below, renders
# standard Markdown, `Pages` and `ui.Keyboard`, and sorts a call's error into one of
# the outcomes here. A window key is a string; the key prefix picks the adapter.
#
# Adapter calls: send(window, text, buttons, reply_to, silent=, thread=) -> message id;
# edit(window, message id, text, buttons) (text None: buttons only); delete(window,
# message id); send_file(window, path, caption); send_photo(window, data, caption,
# buttons=, name=); edit_photo(window, message id, data, buttons=, name=);
# download(attachment, dest); create_window(title) -> key; rename_window(window,
# title); close_window(window); window_alive(window, title) -> bool; answer_tap(tap);
# identity() -> bot name; bind_owner(user id); render(markdown) -> (rich or None,
# plain); render_pages(pages) -> [(rich or None, plain)]; measure(markdown) ->
# rendered size; page_size(head, body, first) -> rendered page size; classify(error);
# run(on message, on tap); close() (after the drain). Attributes: platform, bot_name,
# key_prefix, general, owner_id, command_prefix, pages_in_thread, renders_tables,
# max_message, safe_chunk, photo_max, upload_max, download_max.


@dataclass
class Attachment:
    """A file on an inbound message: its name ("" when the platform gives none), its
    kind ("photo" or "document") and the adapter's handle to download it."""

    name: str
    kind: str
    handle: object


@dataclass
class Inbound:
    """One user message, as an adapter hands it to the kernel. `text` keeps its raw
    whitespace; `mentioned_user_ids` are the users it names (the bot left out);
    `in_thread` marks a message posted in a reply thread (it never moves a bottom frame);
    `echo` marks a command the platform did not show in the window."""

    window: str
    message_id: int | str | None
    user_id: int | str | None
    text: str
    mentioned: bool = False
    replied_text: str | None = None
    replied_author: str | None = None
    replied_user_id: int | str | None = None
    replied_attachments: list[Attachment] = field(default_factory=list)
    quote: str | None = None
    forward_source: str | None = None
    album: str | None = None
    attachments: list[Attachment] = field(default_factory=list)
    mentioned_user_ids: list[int | str] = field(default_factory=list)  # users named in it
    in_thread: bool = False
    echo: bool = False


@dataclass
class Tap:
    """A button tap: the window and message it was on, the button's data, the tapping
    user, and the platform's id for answering it."""

    window: str
    message_id: int | str | None
    data: str
    user_id: int | str | None
    tap_id: str = ""


@dataclass
class Flood:
    """The platform asked for no calls for `seconds`."""

    seconds: float


@dataclass
class Transient:
    """A network error or a server-side failure: worth a bounded retry."""


@dataclass
class NotModified:
    """An edit whose content already matches the message."""


@dataclass
class NotFound:
    """The message to delete is not there."""


@dataclass
class Refused:
    """Any other rejection."""


# ── Transport: one platform's calls + frame-skip output ────────────


@dataclass
class _Frame:
    """The newest wanted content of one live message; the frame task pushes it."""

    window: str
    message_id: int | str | None  # None: a bottom frame with no copy on screen
    plain: str | None  # None: a bottom frame to delete
    md: object = None  # standard Markdown or `ui.Pages`
    reply_markup: object = None
    must_land: bool = False  # pushed ahead of the other frames
    bottom: bool = False  # kept as its window's bottom-most message
    resend: bool = False  # a bottom frame buried under a newer message
    waiters: list = field(default_factory=list)  # futures resolved when this content lands
    shown: tuple | None = None  # the content that last landed
    failures: int = 0  # consecutive failed (non-flood) re-sends of a bottom frame
    vacated: tuple | None = None  # a moved bottom frame's (old id, delete issued at)


@dataclass
class _Title:
    """The newest wanted name of one window."""

    window: str
    name: str
    immediate: bool = False  # an explicit rename: no TITLE_MIN_INTERVAL wait


class Transport:
    """One platform's adapter behind one pacer and one flood-wait deadline, in two kinds
    of calls.

    Essential calls (`_call`: new messages, deletes, one-off edits) are never skipped:
    they go out in issue order, wait for a pacer slot, wait out a flood-wait in full
    (never retried early) and retry until they land.

    Frame-skip output (`show_frame`, `show_bottom_frame`, `show_title`) is for anything
    updated over time: the call only records the newest wanted content and returns at
    once. One frame task pushes each recorded content when a slot is spare (no essential
    call pending) and no flood-wait runs; content replaced before it was pushed is
    skipped, never sent, so local work never waits on the platform.
    """

    def __init__(self, adapter, pace_per_min: float = 0, pace_burst: float = PACE_BURST):
        self.adapter = adapter
        self.flood_until = 0.0  # monotonic deadline of the last flood-wait: no call before it
        self._pacer = _Pacer(pace_per_min, pace_burst)
        self._send_order = asyncio.Lock()  # FIFO: essential calls go out in issue order
        # window → the newest message id seen there, sent or received (ids grow within a
        # window, so any message above a given id landed below that message)
        self.latest_message_ids: dict[str, int | str] = {}
        self._frames: dict[tuple, _Frame] = {}
        self._titles: dict[str, _Title] = {}
        self._title_landed_at: dict[str, float] = {}
        # keys awaiting a push, oldest first → (first recorded at, times replaced since)
        self._dirty: dict[tuple, tuple[float, int]] = {}
        self._frame_wakeup = asyncio.Event()
        self._frames_idle = asyncio.Event()
        self._frames_idle.set()
        self._frame_task: asyncio.Task | None = None
        self._pushing = False  # a push is in flight (its key already left `_dirty`)
        self.on_title_landed = None  # called with (window, name) once a rename lands
        self.bot_name = ""  # the adapter's identity, learned at startup

    def note_message(self, window: str, message_id) -> None:
        """Record `message_id` as seen in `window`; a replayed message (a tapped
        suggestion) may carry no id. A bottom frame it lands below is re-sent, and so
        is one with no copy on screen (its re-send gave up after the old copy's delete)."""
        if message_id is None:
            return
        latest = self.latest_message_ids.get(window)
        if latest is not None and message_id <= latest:
            return
        self.latest_message_ids[window] = message_id
        for key, frame in self._frames.items():
            if (
                frame.bottom
                and frame.window == window
                and frame.plain is not None
                and (frame.message_id is None or frame.message_id < message_id)
            ):
                frame.resend = True
                self._mark_dirty(key)

    def bury_bottom_frames(self, window: str) -> None:
        """Mark every bottom frame of `window` buried: a message the bot never receives
        (a rename's notice) landed below them."""
        for key, frame in self._frames.items():
            if frame.bottom and frame.window == window and frame.plain is not None:
                frame.resend = True
                self._mark_dirty(key)

    def enable_pacing(self):
        """Turn on outbound rate pacing for the live bot. Off by default so tests never
        real-sleep; `App.start` calls this."""
        self._pacer = _Pacer(PACE_MAX_PER_MIN, PACE_BURST)

    # ── One attempt / essential calls ──────────────────────────────

    async def _attempt(
        self,
        make_call,
        ok_not_modified=False,
        spare=True,
        paced=True,
        sent_to=None,
        delete_outcome=False,
    ):
        """Issue one adapter call (its slot already taken). Returns its result,
        `_FLOODED` on a flood-wait (the deadline is recorded), `_NOT_MODIFIED` on a benign
        "not modified" edit when `ok_not_modified`, or None on any other rejection. A
        `Transient` error is retried up to TRANSIENT_RETRIES times (1, 2, 4 s apart),
        each retry clearing the flood deadline and taking a pacer slot like the first try
        (`_clear_to_call` in the caller's mode: a spare slot for the frame task, the
        default; `_call` passes its own); then given up with a warning; any other error
        gets one try. A retried send whose first try did reach the platform can post
        twice. `sent_to` (a send's window) records the new message there, so the window's
        bottom frames move below it. `delete_outcome` (a delete) replaces None with
        `_GONE` (not found), `_UNSURE` (transient retries ran out) or `_REFUSED`."""
        backoff = TRANSIENT_BACKOFF
        for attempt in range(TRANSIENT_RETRIES + 1):
            try:
                result = await make_call()
            except Exception as e:
                kind = self.adapter.classify(e)
                if kind is None:
                    raise
                if isinstance(kind, Flood):
                    LOGGER.warning("flood-wait %ss", kind.seconds)
                    self.flood_until = max(self.flood_until, time.monotonic() + kind.seconds)
                    return _FLOODED
                if isinstance(kind, Transient):
                    if attempt == TRANSIENT_RETRIES:
                        LOGGER.warning("call failed after %d retries: %r", attempt, e)
                        return _UNSURE if delete_outcome else None
                    LOGGER.warning("call failed (%r), retry in %.0fs", e, backoff)
                    await asyncio.sleep(backoff)
                    backoff *= 2
                    await self._clear_to_call(spare=spare, paced=paced)
                    continue
                if isinstance(kind, NotModified) and ok_not_modified:
                    return _NOT_MODIFIED
                LOGGER.debug("call rejected: %r", e)
                if delete_outcome:
                    return _GONE if isinstance(kind, NotFound) else _REFUSED
                return None
            break
        if sent_to is not None and result is not None:
            self.note_message(sent_to, result)
        return result

    async def _wait_flood(self) -> None:
        delay = self.flood_until - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)

    async def _clear_to_call(self, spare: bool = False, paced: bool = True) -> None:
        """Wait until a call may go out: no flood-wait running and, when `paced`, a
        pacer slot taken (a spare one for a frame push). The deadline is re-checked
        after the slot wait — a flood recorded meanwhile sends the slot back and the
        wait starts over — so no call is ever made inside a flood window."""
        while True:
            await self._wait_flood()
            if paced:
                await (self._pacer.take_spare() if spare else self._pacer.take())
            if self.flood_until <= time.monotonic():
                return
            if paced:
                self._pacer.give_back()

    async def _call(self, make_call, ok_not_modified=False, paced=True, sent_to=None):
        """An essential adapter call; returns its result. `make_call` is a zero-arg
        factory (`lambda: self.adapter.send(...)`) so a retry issues a fresh coroutine —
        awaiting the same one twice is a RuntimeError. Calls go out in issue order; a
        flood-waited call waits the full flood and retries until it lands. Swallows other
        rejections, returning None (or `_NOT_MODIFIED`, see `_attempt`). `paced=False` (a
        button-tap answer, which posts no message) skips the pacer slot and the order
        queue but still waits out a flood-wait."""
        self._pacer.waiting += 1
        try:
            async with self._send_order if paced else contextlib.nullcontext():
                while True:
                    await self._clear_to_call(paced=paced)
                    result = await self._attempt(
                        make_call,
                        ok_not_modified,
                        spare=False,
                        paced=paced,
                        sent_to=sent_to,
                    )
                    if result is not _FLOODED:
                        return result
        finally:
            self._pacer.waiting -= 1

    # ── Rendering ──────────────────────────────────────────────────

    def _rich(self, md):
        """The adapter's rich render of standard Markdown, or of the first message of
        `Pages` (only that one rendered); None when it does not fit (the plain body goes
        instead)."""
        if isinstance(md, ui.Pages):
            return self.adapter.render_pages(_first_page(md))[0][0]
        return self.adapter.render(md)[0]

    def _plain(self, plain, md) -> str:
        """The caller's plain body, else the plain render of `md`."""
        if plain is not None:
            return plain
        if isinstance(md, ui.Pages):
            return self.adapter.render_pages(_first_page(md))[0][1]
        return self.adapter.render(md)[1]

    # ── Frame-skip output ──────────────────────────────────────────

    def show_frame(self, window, message_id, plain, md=None, reply_markup=None) -> None:
        """Record the newest content of a live message and return at once (nothing to
        await: a newer frame replaces it, or it lands when a slot is spare)."""
        self._record_frame(window, message_id, plain, md, reply_markup)

    def land_frame(self, window, message_id, plain, md=None, reply_markup=None) -> asyncio.Future:
        """Record a finalizing edit: pushed ahead of the other frames, kept first and
        pending through flood-waits until it lands. `md` may be `Pages` (its first
        message lands). The future resolves True once this content (or a newer one)
        landed, False if the platform rejected it."""
        frame = self._record_frame(window, message_id, plain, md, reply_markup)
        frame.must_land = True
        waiter = asyncio.get_running_loop().create_future()
        frame.waiters.append(waiter)
        return waiter

    def _record_frame(self, window, message_id, plain, md, reply_markup) -> _Frame:
        plain = self._plain(plain, md)
        key = ("msg", window, message_id)
        frame = self._frames.get(key)
        if frame is None:
            frame = _Frame(window=window, message_id=message_id, plain=plain)
            self._frames[key] = frame
            self._forget_old_frames()
        frame.plain, frame.md, frame.reply_markup = plain, md, reply_markup
        self._mark_dirty(key)
        return frame

    def show_bottom_frame(self, window, key: str, plain, md=None, reply_markup=None) -> None:
        """Keep a message as the bottom-most of its window: the first content sends it
        (silently), later ones edit it, and a newer message in the window re-sends it
        below and deletes the old copy. `plain=None` deletes it. Returns at once."""
        frame_key = ("bottom", key)
        frame = self._frames.get(frame_key)
        if frame is None:
            if plain is None:
                return
            frame = _Frame(window=window, message_id=None, plain=plain)
            frame.bottom = True
            self._frames[frame_key] = frame
        frame.plain, frame.md, frame.reply_markup = plain, md, reply_markup
        self._mark_dirty(frame_key)

    def bottom_frame_id(self, key: str):
        """The message id currently holding a bottom frame (None if not sent)."""
        frame = self._frames.get(("bottom", key))
        return frame.message_id if frame else None

    def show_title(self, window: str, name: str, immediate=False) -> None:
        """Record a window's newest wanted name and return at once; renames of one window
        land at least TITLE_MIN_INTERVAL apart (each posts a rename notice), so a burst
        gives one rename to the last name. `immediate` skips that wait (an explicit
        rename)."""
        self._titles[window] = _Title(window, name, immediate)
        self._mark_dirty(("title", window))

    def drop_title(self, window: str) -> None:
        """Forget a recorded rename (the wanted name is already the window's)."""
        self._titles.pop(window, None)
        self._dirty.pop(("title", window), None)

    def _title_landed(self, window: str, name: str) -> None:
        # the rename posts a notice we never receive: the bottom frames sit above it
        self.bury_bottom_frames(window)
        if self.on_title_landed is not None:
            self.on_title_landed(window, name)

    async def flush_frames(self) -> None:
        """Wait until every recorded frame and title has been pushed."""
        while self._dirty or self._pushing:
            self._ensure_frame_task()
            self._frames_idle.clear()
            await self._frames_idle.wait()

    def drop_window_frames(self, window: str) -> None:
        """Forget every pending frame of a window (it went away)."""
        for key in [k for k, frame in self._frames.items() if frame.window == window]:
            self._drop_frame(key)

    def _drop_frame(self, key: tuple) -> None:
        frame = self._frames.pop(key, None)
        self._dirty.pop(key, None)
        if frame is not None:
            _resolve(frame.waiters, False)

    def _forget_old_frames(self) -> None:
        """Bound the memory of landed message frames (every live card leaves one)."""
        idle = [k for k, f in self._frames.items() if k[0] == "msg" and k not in self._dirty]
        for key in idle[: max(0, len(idle) - FRAME_MEMORY)]:
            del self._frames[key]

    def _mark_dirty(self, key: tuple) -> None:
        recorded_at, replaced = self._dirty.get(key, (time.monotonic(), -1))
        self._dirty[key] = (recorded_at, replaced + 1)
        self._frames_idle.clear()
        self._frame_wakeup.set()
        self._ensure_frame_task()

    def _ensure_frame_task(self) -> None:
        if self._frame_task is None or self._frame_task.done():
            self._frame_task = asyncio.get_running_loop().create_task(self._push_frames())

    def _next_dirty(self) -> tuple | None:
        """The next key to push: a must-land frame first, else the oldest one ready (a
        title waits out TITLE_MIN_INTERVAL since its window's last rename)."""
        for key in self._dirty:
            frame = self._frames.get(key)
            if frame is not None and frame.must_land:
                return key
        now = time.monotonic()
        for key in self._dirty:
            if key[0] != "title" or now >= self._title_ready_at(key[1]):
                return key
        return None

    def _title_ready_at(self, window: str) -> float:
        """When a window's recorded rename may go out: TITLE_MIN_INTERVAL after its last
        rename (an immediate one: now)."""
        title = self._titles.get(window)
        if title is None or title.immediate:
            return 0.0
        return self._title_landed_at.get(window, -TITLE_MIN_INTERVAL) + TITLE_MIN_INTERVAL

    def _title_wait(self) -> float | None:
        """Seconds until the earliest waiting title is ready (None when none waits)."""
        waits = [
            self._title_ready_at(key[1]) - time.monotonic()
            for key in self._dirty
            if key[0] == "title"
        ]
        return max(0.0, min(waits)) if waits else None

    async def _push_frames(self) -> None:
        """The frame task: push the newest recorded content when a slot is spare. A
        bottom-frame move needs a second slot free at the same moment; without it the
        slot goes back and the pick is redone once the pacer refills, so a must-land
        frame recorded meanwhile goes first."""
        slot_wait_from = None
        while True:
            if self._next_dirty() is None:
                if not self._dirty:
                    self._frames_idle.set()  # nothing pending and no push in flight
                self._frame_wakeup.clear()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._frame_wakeup.wait(), self._title_wait())
                continue
            if slot_wait_from is None:
                slot_wait_from = time.monotonic()
            await self._clear_to_call(spare=True)
            key = self._next_dirty()  # re-pick: the newest state after the waits
            if key is None:
                self._pacer.give_back()
                continue
            if self._is_move(key) and self._pacer.fits_pair and not self._pacer.take_spare_now():
                self._pacer.give_back()
                self._frame_wakeup.clear()  # a frame recorded meanwhile re-picks at once
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._frame_wakeup.wait(), self._pacer.spare_delay(2))
                continue
            recorded_at, replaced = self._dirty.pop(key)
            pushed_at = time.monotonic()
            self._pushing = True
            try:
                await self._push(key)
            except Exception:
                LOGGER.exception("frame push failed (%s)", key)
            finally:
                self._pushing = False
            LOGGER.info(
                "frame push %s: %.1fs after first recorded (slot wait %.1fs, call %.1fs, "
                "%d replaced, %d still pending)",
                self._frame_label(key),
                pushed_at - recorded_at,
                pushed_at - slot_wait_from,
                time.monotonic() - pushed_at,
                replaced,
                len(self._dirty),
            )
            slot_wait_from = None

    def _is_move(self, key: tuple) -> bool:
        """Whether pushing `key` moves a buried bottom-frame copy (a delete + a send)."""
        frame = self._frames.get(key)
        return (
            frame is not None
            and frame.bottom
            and frame.plain is not None
            and frame.message_id is not None
            and frame.resend
        )

    def _frame_label(self, key: tuple) -> str:
        frame = self._frames.get(key)
        window = f" thread {frame.window}" if frame else ""
        if key[0] == "msg":
            return f"msg {key[2]}{window}"
        if key[0] == "bottom":
            return f"bottom {key[1]}{window}"
        return f"title thread {key[1]}"

    async def _push(self, key: tuple) -> None:
        if key[0] == "title":
            await self._push_title(key)
        else:
            await self._push_frame(key)

    async def _push_title(self, key: tuple) -> None:
        title = self._titles.get(key[1])
        if title is None:
            return
        result = await self._attempt(lambda: self.adapter.rename_window(title.window, title.name))
        if self._titles.get(key[1]) is not title:
            return  # a newer name arrived meanwhile; it is already queued
        if result is _FLOODED:
            self._mark_dirty(key)
            return
        del self._titles[key[1]]
        if result is not None:
            self._title_landed_at[title.window] = time.monotonic()
            self._title_landed(title.window, title.name)

    async def _push_frame(self, key: tuple) -> None:
        frame = self._frames.get(key)
        if frame is None:
            return
        if frame.plain is None:  # a bottom frame to retire
            if frame.message_id is not None:
                window, message_id = frame.window, frame.message_id
                result = await self._attempt(lambda: self.adapter.delete(window, message_id))
                if result is _FLOODED:
                    self._mark_dirty(key)
                    return
            if self._frames.get(key) is frame and frame.plain is None:
                del self._frames[key]
            return
        content = (frame.plain, frame.md, frame.reply_markup)
        waiters, frame.waiters = frame.waiters, []
        must_land, frame.must_land = frame.must_land, False  # a newer must-land sets it anew
        if frame.bottom and (frame.message_id is None or frame.resend):
            await self._resend_bottom(key, frame)
            return
        if content == frame.shown:
            _resolve(waiters, True)
            return
        result = await self._edit_attempt(frame.window, frame.message_id, *content)
        if result is _FLOODED:
            frame.waiters = waiters + frame.waiters
            frame.must_land = frame.must_land or must_land  # stays first until it lands
            self._mark_dirty(key)
        elif result is None:
            _resolve(waiters, False)
        else:
            frame.shown = content
            _resolve(waiters, True)

    async def _resend_bottom(self, key: tuple, frame: _Frame) -> None:
        """Send a bottom frame below the window's newest message. A buried copy moves
        with the two slots the frame task holds (one slot each when the burst is under
        2): the old copy's delete, then the new send right after it, so the window never
        shows two copies and the gap without one is one call. Only a landed delete or
        "not found" frees the old copy; an undeletable one is edited to a stub first."""
        if frame.message_id is not None:
            if not await self._vacate_bottom(key, frame):
                return
            if self._frames.get(key) is not frame or frame.plain is None:
                self._pacer.give_back()  # dropped or retired during the delete: no send
                return
        content = (frame.plain, frame.md, frame.reply_markup)  # the newest, read at the send
        plain, md, markup = content
        window = frame.window
        result = None
        rich = self._rich(md) if md is not None else None
        if rich is not None:
            result = await self._attempt(
                lambda: self.adapter.send(window, rich, markup, None, silent=True),
                sent_to=window,
            )
            if result is None:  # the rich body was rejected: a second, plain attempt
                await self._clear_to_call(spare=True)
        if result is None:
            result = await self._attempt(
                lambda: self.adapter.send(window, _cap(plain), markup, None, silent=True),
                sent_to=window,
            )
        if result is _FLOODED:
            LOGGER.info("bottom frame %s: re-send flood-waited, retrying", key[1])
            self._mark_dirty(key)
            return
        if result is None:
            self._count_bottom_failure(key, frame, "re-send")
            return
        frame.failures = 0
        frame.message_id = result
        frame.shown = content
        old, deleted_at = frame.vacated or (None, None)
        frame.vacated = None
        gap = "" if deleted_at is None else f" (gap {time.monotonic() - deleted_at:.1f}s)"
        LOGGER.info("bottom frame %s: re-anchored %s → %s%s", key[1], old, frame.message_id, gap)
        latest = self.latest_message_ids.get(window)
        frame.resend = latest is not None and latest > frame.message_id
        if frame.resend or (frame.plain, frame.md, frame.reply_markup) != content:
            self._mark_dirty(key)
        else:  # the send's own id marked it buried; nothing is left to push
            self._dirty.pop(key, None)

    async def _vacate_bottom(self, key: tuple, frame: _Frame) -> bool:
        """Take a buried bottom-frame copy off the screen ahead of its re-send. Called
        holding the delete's slot, plus the send's when the pacer fits a pair. True once
        the copy is gone (deleted, not found, or edited to a stub and untracked), with
        the send's slot held. False when the copy stays, holding no slot: a flood-wait
        retries the move, an unconfirmed delete or a failed stub counts as a failure."""
        paired = self._pacer.fits_pair
        window, old = frame.window, frame.message_id
        deleted_at = time.monotonic()
        outcome = await self._attempt(lambda: self.adapter.delete(window, old), delete_outcome=True)
        if outcome is _FLOODED or outcome is _UNSURE:
            if paired:
                self._pacer.give_back()
            if outcome is _FLOODED:
                LOGGER.info("bottom frame %s: delete of %s flood-waited, retrying", key[1], old)
                self._mark_dirty(key)
            else:
                LOGGER.info("bottom frame %s: delete of %s unconfirmed", key[1], old)
                self._count_bottom_failure(key, frame, "delete")
            return False
        if outcome is _GONE:
            LOGGER.info("bottom frame %s: old copy %s already gone", key[1], old)
        elif outcome is _REFUSED:
            if paired:
                await self._recheck_flood()
            else:
                await self._clear_to_call(spare=True)
            stub = await self._attempt(
                lambda: self.adapter.edit(window, old, STUB_TEXT, None),
                ok_not_modified=True,
            )
            if stub is None or stub is _FLOODED:
                LOGGER.warning(
                    "bottom frame %s: old copy %s can't be deleted or stubbed", key[1], old
                )
                if stub is _FLOODED:
                    self._mark_dirty(key)
                else:
                    self._count_bottom_failure(key, frame, "stub")
                return False
            LOGGER.warning("bottom frame %s: old copy %s can't be deleted, stubbed", key[1], old)
            paired = False  # the stub took the send's slot
        if paired:
            await self._recheck_flood()
        else:
            await self._clear_to_call(spare=True)
        frame.message_id, frame.vacated = None, (old, deleted_at)
        return True

    async def _recheck_flood(self) -> None:
        """A slot held across another call: if a flood-wait began meanwhile, hand it
        back and wait the flood out for a fresh one."""
        if self.flood_until > time.monotonic():
            self._pacer.give_back()
            await self._clear_to_call(spare=True)

    def _count_bottom_failure(self, key: tuple, frame: _Frame, what: str) -> None:
        """A failed (non-flood) bottom-frame move or re-send: retried up to
        BOTTOM_RESEND_ATTEMPTS times, then left for the next content or newer message."""
        frame.failures += 1
        if frame.failures < BOTTOM_RESEND_ATTEMPTS:
            LOGGER.info("bottom frame %s: %s failed, retrying", key[1], what)
            self._mark_dirty(key)
        else:
            LOGGER.warning("bottom frame %s: %s failed %d times", key[1], what, frame.failures)
            frame.failures = 0

    async def _edit_attempt(self, window, msg_id, plain, md, reply_markup):
        """One edit of a frame: the rich render when it fits, else (or when the platform
        rejects it) the plain body. An over-limit render is never sent: a hard cut could
        land on valid markup and silently drop the final words."""
        rich = self._rich(md) if md is not None else None
        if rich is not None:
            result = await self._attempt(
                lambda: self.adapter.edit(window, msg_id, rich, reply_markup),
                ok_not_modified=True,
            )
            if result is not None:
                return result
            await self._clear_to_call(spare=True)
        return await self._attempt(
            lambda: self.adapter.edit(window, msg_id, _cap(plain), reply_markup),
            ok_not_modified=True,
        )

    # ── Essential sends / edits ────────────────────────────────────

    async def send(self, window, text, reply_to=None, buries_bottom=True, reply_markup=None):
        """Send plain text, split to fit; `reply_markup` rides the last piece."""
        mid = None
        pieces = chunks(text)
        sent_to = window if buries_bottom else None
        for i, chunk in enumerate(pieces):
            buttons = reply_markup if i == len(pieces) - 1 else None
            reply = reply_to if i == 0 else None
            result = await self._call(
                lambda chunk=chunk, buttons=buttons, reply=reply: self.adapter.send(
                    window, _cap(chunk), buttons, reply
                ),
                sent_to=sent_to,
            )
            mid = result if result is not None else mid
        return mid

    async def edit(self, window, msg_id, text, reply_markup=None) -> bool:
        """Replace a message's text (plain); its buttons become `reply_markup`."""
        result = await self._call(
            lambda: self.adapter.edit(window, msg_id, _cap(text), reply_markup),
            ok_not_modified=True,
        )
        return result is not None

    async def set_buttons(self, window, msg_id, reply_markup=None) -> bool:
        """Replace a message's buttons, its text unchanged (None: no buttons)."""
        result = await self._call(
            lambda: self.adapter.edit(window, msg_id, None, reply_markup),
            ok_not_modified=True,
        )
        return result is not None

    async def send_rich(self, window, text, reply_to=None, reply_markup=None):
        """Send standard Markdown, rendered by the adapter (plain fallback per piece),
        split to fit; the `reply_markup` goes on the last piece."""
        mid = None
        pieces = md_chunks(text, self.adapter.measure, self.adapter.safe_chunk)
        for i, raw in enumerate(pieces):
            buttons = reply_markup if i == len(pieces) - 1 else None
            reply = reply_to if i == 0 else None
            result = await self._send_rendered(window, raw, None, buttons, reply)
            mid = result if result is not None else mid
        return mid

    async def _send_rendered(self, window, md, plain, buttons, reply_to, silent=False):
        """One message: the rich render of `md` when it fits, else (or when the platform
        rejects it) the plain body."""
        rendered_rich, rendered_plain = self.adapter.render(md)
        plain = rendered_plain if plain is None else plain
        result = None
        if rendered_rich is not None:
            result = await self._call(
                lambda: self.adapter.send(window, rendered_rich, buttons, reply_to, silent=silent),
                sent_to=window,
            )
        if result is None:
            result = await self._call(
                lambda: self.adapter.send(window, _cap(plain), buttons, reply_to, silent=silent),
                sent_to=window,
            )
        return result

    async def send_md(
        self,
        window,
        md,
        plain=None,
        reply_to=None,
        silent=False,
        reply_markup=None,
        first_id=None,
    ):
        """Send standard Markdown with a plain fallback (mirrors `edit_md`); the caller
        keeps both within MAX_MSG — no chunking. `md` may be `Pages`: each message per
        the adapter's layout, the later ones in the first one's thread where the adapter
        threads pages; `first_id` is a first message already on screen (a landed card),
        so only the rest are sent. Returns the first message's id. `silent` sends without
        a notification."""
        if not isinstance(md, ui.Pages):
            return await self._send_rendered(window, md, plain, reply_markup, reply_to, silent)
        messages = self.adapter.render_pages(md)
        if first_id is None:
            first_rich, first_plain = messages[0]
            first_id = await self._send_pages_message(
                window, first_rich, first_plain, reply_to, None, silent
            )
        thread = first_id if self.adapter.pages_in_thread else None
        for rich, page_plain in messages[1:]:
            await self._send_pages_message(window, rich, page_plain, None, thread, silent)
        return first_id

    async def _send_pages_message(self, window, rich, plain, reply_to, thread, silent):
        """One page of `Pages`; a page posted in a thread is not a new message of the
        window's timeline, so it moves no bottom frame."""
        sent_to = None if thread is not None else window
        result = None
        if rich is not None:
            result = await self._call(
                lambda: self.adapter.send(
                    window, rich, None, reply_to, silent=silent, thread=thread
                ),
                sent_to=sent_to,
            )
        if result is None:
            result = await self._call(
                lambda: self.adapter.send(
                    window, _cap(plain), None, reply_to, silent=silent, thread=thread
                ),
                sent_to=sent_to,
            )
        return result

    async def edit_rich(self, window, msg_id, text) -> bool:
        """Edit a message to standard Markdown (plain fallback)."""
        return await self.edit_md(window, msg_id, text, text)

    async def edit_md(self, window, msg_id, md, plain, reply_markup=None) -> bool:
        """Edit a message to standard Markdown with a caller-bounded plain fallback. An
        over-limit render goes straight to the plain body, never cut."""
        rich = self._rich(md)
        result = None
        if rich is not None:
            result = await self._call(
                lambda: self.adapter.edit(window, msg_id, rich, reply_markup),
                ok_not_modified=True,
            )
        if result is not None:
            return True
        return await self.edit(window, msg_id, self._plain(plain, md), reply_markup=reply_markup)

    async def delete_msg(self, window, msg_id) -> None:
        """Delete a message (its pending frame is dropped); a rejection (the message is
        already gone) counts as done."""
        self._drop_frame(("msg", window, msg_id))
        await self._call(lambda: self.adapter.delete(window, msg_id))

    async def send_photo_b64(self, window, media_type, b64, reply_markup=None):
        raw = base64.b64decode(b64)
        if len(raw) > self.adapter.photo_max:
            return None
        name = f"image.{media_type.split('/')[-1] or 'png'}"
        return await self._upload(
            window,
            lambda: self.adapter.send_photo(window, raw, None, buttons=reply_markup, name=name),
        )

    async def _upload(self, window, make_call):
        """An essential upload. One the platform took without reporting its message id
        still lands below the window's bottom frames: they are marked buried."""
        posted = []

        async def call():
            result = await make_call()
            posted.append(True)
            return result

        result = await self._call(call, sent_to=window)
        if result is None and posted:
            self.bury_bottom_frames(window)
        return result

    async def edit_photo_b64(self, window, msg_id, media_type, b64, reply_markup=None) -> bool:
        """Replace an existing photo message's image in place (keeps one live frame
        instead of a growing pile). Returns False if the edit was rejected."""
        raw = base64.b64decode(b64)
        if len(raw) > self.adapter.photo_max:
            return False
        name = f"image.{media_type.split('/')[-1] or 'png'}"
        result = await self._call(
            lambda: self.adapter.edit_photo(window, msg_id, raw, buttons=reply_markup, name=name)
        )
        return result is not None

    async def download_file(self, attachment, dest: Path) -> Path | None:
        """Download an inbound attachment to `dest`. Returns the path, or None on failure
        (e.g. over the platform's download cap)."""
        try:
            return await self.adapter.download(attachment, dest)
        except Exception as e:
            if self.adapter.classify(e) is None:
                raise
            LOGGER.warning(
                "download failed for %s (over the %dMB cap?)",
                attachment.name or attachment.kind,
                self.adapter.download_max // (1024 * 1024),
            )
            return None

    async def send_file(self, window, path: Path):
        if not path.is_file():
            raise RuntimeError(f"no such file: {path}")
        size = path.stat().st_size
        cap = self.adapter.upload_max
        if size > cap:
            raise RuntimeError(
                f"{path.name} is {size / 1e6:.0f}MB — over the {cap // (1024 * 1024)}MB cap"
            )
        media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if media_type.startswith("image/") and size <= self.adapter.photo_max:
            data = path.read_bytes()
            await self._upload(
                window, lambda: self.adapter.send_photo(window, data, None, name=path.name)
            )
        else:
            await self._upload(window, lambda: self.adapter.send_file(window, path, None))


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
    def __init__(self, core, window, message, text, args, topic, saved, user_id=None):
        self._core = core
        self.window = window  # the window key the event came from
        self.message: Inbound | None = message
        self.text = text
        self.args = args
        self.user_id = user_id  # the acting user's id (None for a service call)
        self.topic: Topic | None = topic
        self.saved: SavedDict = saved  # the per-plugin SavedDict of the handling plugin

    @property
    def is_admin(self) -> bool:
        """True if the acting user is the bound owner of this window's platform."""
        return self.user_id is not None and self.user_id == self._core.owner_for(self.window)

    @property
    def _tx(self) -> Transport:
        return self._core.transport_for(self.window)

    # ── Replies ────────────────────────────────────────────────────
    # Inside a window, new messages and deletes go through the window's own methods,
    # so a window that orders its messages (a Claude window's send steps) sees them.
    async def send(self, text, reply_to=None):
        if self.topic is not None:
            return await self.topic.send(text, reply_to=reply_to)
        return await self._tx.send(self.window, text, reply_to=reply_to)

    async def send_rich(self, text, reply_to=None):
        if self.topic is not None:
            return await self.topic.send_rich(text, reply_to=reply_to)
        return await self._tx.send_rich(self.window, text, reply_to=reply_to)

    async def edit(self, msg_id, text, reply_markup=None):
        return await self._tx.edit(self.window, msg_id, text, reply_markup=reply_markup)

    async def edit_md(self, msg_id, md, plain, reply_markup=None):
        return await self._tx.edit_md(self.window, msg_id, md, plain, reply_markup=reply_markup)

    async def send_file(self, path):
        if self.topic is not None:
            return await self.topic.send_file(path)
        return await self._tx.send_file(self.window, path)

    async def download_file(self, attachment, dest):
        return await self._tx.download_file(attachment, dest)

    async def send_photo(self, media_type, b64):
        if self.topic is not None:
            return await self.topic.send_photo(media_type, b64)
        return await self._tx.send_photo_b64(self.window, media_type, b64)

    async def delete(self, msg_id):
        if self.topic is not None:
            await self.topic.delete(msg_id)
        else:
            await self._tx.delete_msg(self.window, msg_id)

    # ── Subprocesses ───────────────────────────────────────────────
    async def spawn(self, *args, **kwargs):
        """Spawn a child: through the window's `Topic.spawn` (reaped when the window
        closes) inside a window, a plain spawn in General (no window to close)."""
        if self.topic is not None:
            return await self.topic.spawn(*args, **kwargs)
        return await asyncio.create_subprocess_exec(*args, **kwargs)

    # ── Ask ────────────────────────────────────────────────────────
    async def ask_buttons(self, text, options, timeout=300, announce=None, cancel=True):
        return await self._core.ask_buttons(
            self.window,
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
        return await self._core.menu(self.window, title, options)

    async def ask_text(self, prompt=None, timeout=180):
        return await self._core.ask_text(self.window, prompt, timeout=timeout)

    def await_next(self, handler):
        self._core.await_next(self.window, handler)

    # ── Windows + cross-plugin ─────────────────────────────────────
    async def open(self, topic_class, name=None):
        """Create a window of `topic_class` on this window's platform (name deduped by
        the core) and return its live instance."""
        return await self._core.open_window(topic_class, name, origin=self.window)

    async def call_service(self, name, *args):
        """Invoke another plugin's @service for the current window."""
        return await self._core.call_service(name, self.window, *args)

    def has_service(self, name) -> bool:
        return self._core.has_service(name)

    async def offer_handoff(self, message_id, payload, service, label):
        await self._core.offer_handoff(self.window, message_id, payload, service, label)


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
            await ctx.send("run /close inside a window")
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
        await self._kernel.show_menu(ctx.window)

    @universal("/help", "what you can do here", menu="none", inline=True, aliases=("/?",))
    async def help(self, ctx):
        return self._kernel.help_text(ctx.window)

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
        detached_restart(service, self._kernel.config.home_path, announce_window=ctx.window)


class Kernel:
    """The router and window manager over one `Transport` per configured platform."""

    def __init__(
        self,
        config: BotConfig,
        plugins: list | None = None,
        transports: list[Transport] | None = None,
    ):
        self.config = config
        self.transports: list[Transport] = list(transports or [])
        for tx in self.transports:
            tx.on_title_landed = self._title_landed
        self.shutting_down = False  # set on a graceful stop so drivers stay quiet
        self._start_mono = time.monotonic()
        self.db = AppDB(config.db_file)
        self.saved = self.db.saved(core_ns())
        # inline-button prompts awaiting a tap, keyed by prompt id
        self.pending_callbacks: dict[str, asyncio.Future] = {}
        # menu navigation stack per window (the UINavigationController model): a list
        # of screen titles = the depth; the message every level edits; a push counter
        # so a menu can tell whether a tapped command navigated or just acted
        self._nav: dict[str, list[str]] = {}
        self._menu_msg: dict[str, object] = {}
        self._nav_pushes: dict[str, int] = {}
        self._menu_owner: dict[str, object] = {}  # token of the live menu session per window
        self._active_prompt: dict[str, str] = {}  # pid of the tap a window is awaiting now
        # a window awaiting the owner's next plain message (ask_text / await_next)
        self._await_input: dict[str, Fallback] = {}
        # pending handoff taps
        self._handoffs: dict[str, tuple] = {}
        # ownership + live instances
        self.owners: dict[str, str] = {}  # window key -> class id
        self.instances: dict[str, Topic] = {}  # window key -> live Topic
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

    # ── Platforms ──────────────────────────────────────────────────

    def find_transport(self, window: str) -> Transport | None:
        """The transport whose adapter owns `window`: the longest key prefix that leaves
        a bare id (no further `:`); None when no configured platform owns it."""
        best = None
        for tx in self.transports:
            prefix = tx.adapter.key_prefix
            if not window.startswith(prefix) or ":" in window[len(prefix) :]:
                continue
            if best is None or len(prefix) > len(best.adapter.key_prefix):
                best = tx
        return best

    def transport_for(self, window: str) -> Transport:
        tx = self.find_transport(window)
        if tx is None:
            raise KeyError(f"no platform owns window {window!r}")
        return tx

    def is_general(self, window: str) -> bool:
        """Whether `window` is its platform's General (outside any window)."""
        tx = self.find_transport(window)
        return tx is not None and window == tx.adapter.general

    def owner_for(self, window: str):
        """The bound owner of `window`'s platform (None before `/init`)."""
        tx = self.find_transport(window)
        return tx.adapter.owner_id if tx is not None else None

    def enable_pacing(self) -> None:
        for tx in self.transports:
            tx.enable_pacing()

    async def flush_frames(self) -> None:
        """Wait until every platform's recorded frames and titles have been pushed."""
        await asyncio.gather(*(tx.flush_frames() for tx in self.transports))

    # ── Name registry (persisted in the core store) ────────────────

    def _names(self) -> dict[str, str]:
        return dict(self.saved.get("names", {}))

    def _save_names(self, names: dict[str, str]) -> None:
        self.saved["names"] = names

    def _save_owners(self) -> None:
        self.saved["owners"] = dict(self.owners)

    def _dedup_name(self, name: str, tx: Transport) -> str:
        """`name`, or `name #k` when a window on the same platform already has it."""
        existing = {
            title for window, title in self._names().items() if self.find_transport(window) is tx
        }
        if name not in existing:
            return name
        k = 2
        while f"{name} #{k}" in existing:
            k += 1
        return f"{name} #{k}"

    def owner_of(self, window: str | None) -> str | None:
        return self.owners.get(window) if window is not None else None

    # ── Topic + window ops ─────────────────────────────────────────

    async def create_topic(self, name: str, tx: Transport) -> tuple[str, str] | None:
        """Create a window on `tx`'s platform (deduped name). Returns (window, actual)."""
        actual = self._dedup_name(name, tx)
        window = await tx._call(lambda: tx.adapter.create_window(actual))
        if not window:
            return None
        names = self._names()
        names[window] = actual
        self._save_names(names)
        return window, actual

    def _instantiate(self, cls: type[Topic], window: str, name: str) -> Topic:
        saved = self.db.saved(window_ns(cls.id, window))
        inst = cls(self, window, name, saved)
        entry = self.registry.classes.get(cls.id)
        inst.plugin = self.plugin_by_id.get(entry.plugin_id) if entry else None
        self.instances[window] = inst
        return inst

    def _window_title(self, cls: type[Topic], name: str | None) -> str:
        """A window's display name: the class icon + a capitalized base (the given
        name, else the class's menu label or its id title-cased)."""
        base = name or cls.menu_label or cls.id.title()
        if cls.icon and not base.startswith(cls.icon):
            base = f"{cls.icon} {base}"
        return base

    async def open_window(
        self, topic_class: type[Topic], name=None, *, origin: str
    ) -> Topic | None:
        """Open a window of `topic_class` on the platform of `origin`, the window key the
        request came from."""
        tx = self.transport_for(origin)
        created = await self.create_topic(self._window_title(topic_class, name), tx)
        if created is None:
            await tx.send(origin, "couldn't create window")
            return None
        window, actual = created
        self.owners[window] = topic_class.id
        self._save_owners()
        inst = self._instantiate(topic_class, window, actual)
        base = actual
        if topic_class.icon and base.startswith(topic_class.icon):
            base = base[len(topic_class.icon) :].strip()
        inst.saved["base_name"] = base  # the deduped base ("Shell #2") title refresh keeps
        await inst.on_open()
        return inst

    async def adopt(self, window: str, topic_class: type[Topic]) -> Topic:
        """Transform a window (a core window) into `topic_class`, keeping its key, name
        and history. The inverse of release()."""
        old = self.instances.get(window)
        if old is not None:
            await self._teardown(old)
            self.db.drop(window_ns(self.owners.get(window, "core"), window))
        name = self._names().get(window, topic_class.id)
        self.owners[window] = topic_class.id
        self._save_owners()
        inst = self._instantiate(topic_class, window, name)
        await inst.on_open()
        return inst

    async def _teardown(self, inst) -> None:
        """Close a window instance and reap every child it spawned — the one teardown
        path, so no close/release/transform can leak a subprocess or push a frame."""
        await inst.on_close()
        await inst.reap_all()
        self.transport_for(inst.window).drop_window_frames(inst.window)

    async def release_window(self, window: str) -> None:
        inst = self.instances.get(window)
        cls_id = self.owners.get(window, "core")
        if inst is not None:
            await self._teardown(inst)
        self.db.drop(window_ns(cls_id, window))
        self.owners[window] = "core"
        self._save_owners()
        name = self._names().get(window, "window")
        self._instantiate(_CoreTopic, window, name)

    async def close_window(self, window: str) -> None:
        inst = self.instances.get(window)
        cls_id = self.owners.get(window, "core")
        if inst is not None:
            await self._teardown(inst)
        tx = self.transport_for(window)
        closed = await tx._call(lambda: tx.adapter.close_window(window))
        if closed is None:
            # the close failed but the window is still there — keep it owned so it isn't
            # forgotten into an orphaned, unowned window. The reconcile sweep does not
            # retry the close; it only drops this window once it is actually gone
            # (re-run /close to retry the close itself).
            LOGGER.warning("closing window %s failed; kept owned", window)
            return
        self.db.drop(window_ns(cls_id, window))
        self._forget(window)

    def _forget(self, window: str) -> None:
        tx = self.find_transport(window)
        if tx is not None:
            tx.drop_title(window)
        self.instances.pop(window, None)
        self.owners.pop(window, None)
        names = self._names()
        names.pop(window, None)
        self._save_names(names)
        self._save_owners()

    def _compose_title(self, base: str, suffix: str | None) -> str:
        """`<base> · <suffix>` (the icon is added by rename_topic); the bare base when
        there is no suffix. `base` is the window's deduped name, not the class label, so
        a "#2" survives a title refresh."""
        return f"{base} · {suffix}" if suffix else base

    def refresh_title(self, window: str, immediate: bool = False) -> None:
        """Rebuild a window's title from its live `title_suffix()`, keeping its deduped
        base name."""
        inst = self.instances.get(window)
        if inst is not None:
            base = inst.saved.get("base_name") or (type(inst).menu_label or type(inst).id.title())
            title = self._compose_title(base, inst.title_suffix())
            self.rename_topic(window, title, immediate=immediate)

    def rename_topic(self, window: str, name: str, immediate: bool = False) -> None:
        """Record a window's wanted name (a frame-skip title, see `show_title`) and
        return at once; a name equal to the window's current one makes no call."""
        inst = self.instances.get(window)
        if inst and inst.icon and not name.startswith(inst.icon):
            name = f"{inst.icon} {name}"  # a renamed window keeps its class icon
        tx = self.transport_for(window)
        if self._names().get(window) == name:
            tx.drop_title(window)  # unchanged → no API call
            return
        tx.show_title(window, name, immediate=immediate)

    def _title_landed(self, window: str, name: str) -> None:
        names = self._names()
        names[window] = name
        self._save_names(names)
        inst = self.instances.get(window)
        if inst is not None:
            inst.name = name  # keep the live instance's name in sync with the title

    # ── Services + handoff ─────────────────────────────────────────

    async def call_service(self, name: str, window: str, *args) -> None:
        """Invoke a registered @service as handler(ctx, *args) with a context for
        the target window."""
        entry = self.registry.services.get(name)
        if entry is None:
            return
        pid, attr = entry
        plugin = self.plugin_by_id[pid]
        instance = self.instances.get(window)
        ctx = self._ctx(pid, None, window, "", "", instance)
        await getattr(plugin, attr)(ctx, *args)

    def has_service(self, name: str) -> bool:
        return name in self.registry.services

    async def offer_handoff(self, window, message_id, payload, service, label):
        token = uuid.uuid4().hex[:8]
        self._handoffs[token] = (service, window, payload)
        kb = ui.stacked([ui.cta(label, "handoff", token)])
        if not await self.transport_for(window).set_buttons(window, message_id, kb):
            self._handoffs.pop(token, None)

    async def _on_handoff(self, arg: str) -> None:
        info = self._handoffs.pop(arg, None)
        if info is not None:
            service, window, payload = info
            await self.call_service(service, window, payload)

    async def _in_window(self, window, call):
        """Run a message-posting call through the window's own send order."""
        inst = self.instances.get(window)
        return await (inst.run_message_call(call) if inst is not None else call())

    def uptime(self) -> float:
        return time.monotonic() - self._start_mono

    # ── Interaction helpers ────────────────────────────────────────

    async def ask_buttons(self, window, text, options, timeout=300, announce=None, cancel=True):
        """Ask a one-tap choice. A Cancel option is appended by default — pass
        `cancel=False` to omit it, or a string to relabel it; tapping it (or a
        timeout) returns None. `announce(label) -> str` rewrites the prompt in place
        to reflect the pick (buttons drop); without it the buttons are just removed."""
        opts = list(options)
        if cancel:
            opts.append((cancel if isinstance(cancel, str) else "Cancel", CANCEL))
        pid = uuid.uuid4().hex[:8]
        kb = ui.stacked_choices([label for label, _v in opts], pid)
        tx = self.transport_for(window)
        # registered before the prompt is visible, so even an instant tap is caught; the
        # cleanup covers the send too (a window closing while it is queued cancels it)
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        self.pending_callbacks[pid] = fut
        try:
            msg_id = await self._in_window(window, lambda: tx.send(window, text, reply_markup=kb))
            if msg_id is None:
                return None, None  # the prompt never rendered — don't hang for the timeout
            try:
                idx = await asyncio.wait_for(fut, timeout)
            except TimeoutError:
                idx = None
        finally:
            self.pending_callbacks.pop(pid, None)
        await self._settle_prompt(window, msg_id, opts, idx, announce)
        value = opts[idx][1] if idx is not None else None
        return (None if value == CANCEL else value), msg_id

    async def _settle_prompt(self, window, message_id, options, idx, announce) -> None:
        tx = self.transport_for(window)
        if announce is not None:
            text = announce(options[idx][0]) if idx is not None else "⏱ timed out"
            await tx.edit(window, message_id, text)
        else:
            await tx.set_buttons(window, message_id, None)

    async def edit_text(self, window, message_id, text) -> None:
        """Rewrite a prior message in place (buttons drop) — used to announce the
        result of a tapped menu once its action has run."""
        if message_id is None:
            return
        await self.transport_for(window).edit(window, message_id, text)

    async def ask_text(self, window, prompt=None, timeout=180):
        """Await a typed reply. A prompt carries a ✖ Cancel button; tapping it (or a
        timeout) resolves None. The typed feeder and the tap share one future — a str
        result is the text, an int result is the Cancel tap."""
        fut: asyncio.Future = asyncio.get_event_loop().create_future()

        async def _feed(text):
            if not fut.done():
                fut.set_result(text)

        pid = uuid.uuid4().hex[:8]
        self._await_input[window] = _feed
        tx = self.transport_for(window)
        msg_id = None
        result = None
        try:  # the cleanup covers the prompt's send too (it can be cancelled while queued)
            if prompt:
                kb = ui.stacked_choices(["✖ Cancel"], pid)
                self.pending_callbacks[pid] = fut  # before the prompt is visible
                msg_id = await self._in_window(
                    window, lambda: tx.send(window, prompt, reply_markup=kb)
                )
                if msg_id is None:
                    return None  # the prompt never rendered — don't hang for the timeout
            try:
                result = await asyncio.wait_for(fut, timeout)
            except TimeoutError:
                result = None
        finally:
            if self._await_input.get(window) is _feed:
                del self._await_input[window]
            self.pending_callbacks.pop(pid, None)
            await self._strip_markup(window, msg_id)
        return None if isinstance(result, int) else result

    def await_next(self, window: str, handler: Fallback) -> None:
        self._await_input[window] = handler

    # ── Help + usage text ──────────────────────────────────────────

    def help_text(self, window: str) -> str:
        """What can be typed here, each command quoted with the platform's command
        prefix (`/` on Telegram, `/tgf ` on Slack)."""
        tx = self.find_transport(window)
        prefix = tx.adapter.command_prefix if tx is not None else "/"

        def typed(name: str) -> str:
            return prefix + name.lstrip("/")

        # a one-token command (`/menu`) is a link in the chat; one after a slash command
        # (`/tgf menu`) can only be typed
        verb = "Type" if prefix.endswith(" ") else "Tap"
        cls_id = self.owner_of(window)
        lines: list[str] = [
            f"{verb} {prefix.strip()} (or {typed('/menu')}) for the button menu. Or type:"
        ]
        if cls_id and cls_id in self.registry.classes and cls_id != "core":
            entry = self.registry.classes[cls_id]
            if entry.commands:
                lines.append(f"commands in this {cls_id} window:")
                for name, (_attr, help, _icon, _inline) in sorted(entry.commands.items()):
                    lines.append(f"  {typed(name)} — {help}")
        lines.append("commands anywhere:")
        for name, (_pid, _attr, help, _icon, _menu, _inline) in sorted(
            self.registry.universals.items()
        ):
            lines.append(f"  {typed(name)} — {help}")
        if not cls_id or cls_id == "core":
            lines.append("open a window:")
            for name, (_cid, help) in sorted(self.registry.launches.items()):
                lines.append(f"  {typed(name)} — {help}")
        return "\n".join(lines)

    def _menu_header(self, window: str) -> str:
        """The menu's title — the window's own icon+name in a window, else a neutral
        desktop title. Never a lone emoji (no jumbo render)."""
        cls_id = self.owner_of(window)
        if cls_id and cls_id != "core" and cls_id in self.registry.classes:
            return self.registry.classes[cls_id].cls.menu_entry()
        return ui.DESKTOP_MENU_TITLE

    def _allowed(self, instance, kind: str, name: str) -> bool:
        """A window's own say over which commands run in it (default: all)."""
        return instance is None or instance.command_allowed(kind, name)

    def _menu_options(self, window: str) -> list[tuple[str, str]]:
        """The buttons for this context: the current window's own commands, or the
        launchers on the desktop, plus the universals allowed here — each with its
        icon (a fallback if it declared none). The window filters its own set via
        `command_allowed`. The kernel curates nothing."""
        cls_id = self.owner_of(window)
        instance = self.instances.get(window)
        in_window = not self.is_general(window)
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

    async def _prompt_tap(self, window, text, options, msg_id):
        """Render a one-tap keyboard — a fresh message, or in place on `msg_id` —
        and await the tap. Returns (value, msg_id); the buttons are left for the
        caller to re-render (a menu loops, a leaf swaps to Back)."""
        pid = uuid.uuid4().hex[:8]
        solo_back = bool(options) and options[-1][1] == MENU_BACK
        kb = ui.stacked_choices([label for label, _v in options], pid, solo_last=solo_back)
        tx = self.transport_for(window)
        # the tap future is registered before the keyboard is visible, so even an instant
        # tap is caught; the prompt becomes the window's active one (which a newer menu
        # supersedes) only once it is on screen; the cleanup covers the send too
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        self.pending_callbacks[pid] = fut
        idx = None
        try:
            if msg_id is None:
                msg_id = await self._in_window(
                    window, lambda: tx.send(window, text, reply_markup=kb)
                )
                if msg_id is None:
                    return None, None  # the menu never rendered — don't hang for the timeout
            else:
                await tx.edit(window, msg_id, text, reply_markup=kb)
            self._active_prompt[window] = pid  # the awaited tap: a newer menu can dismiss it
            try:
                idx = await asyncio.wait_for(fut, 120)
            except TimeoutError:
                idx = None
        finally:
            self.pending_callbacks.pop(pid, None)
            if self._active_prompt.get(window) == pid:
                del self._active_prompt[window]
        return (options[idx][1] if idx is not None else None), msg_id

    def _is_inline(self, choice: str, window: str) -> bool:
        """Whether a tapped entry is inline-able (returns text → render in place with
        Back) vs. one-off (mark triggered, it does its own output). Read from each
        command's own declaration — no list. `open:` (a launcher) is never inline."""
        kind, _, val = choice.partition(":")
        if kind == "cmd":
            entry = self.registry.universals.get(val)
            return bool(entry and entry[5])
        if kind == "ccmd":
            cls_id = self.owner_of(window)
            entry = self.registry.classes.get(cls_id) if cls_id else None
            return bool(entry and val in entry.commands and entry.commands[val][3])
        return False

    def _supersede_prompt(self, window: str) -> None:
        """Dismiss the tap a window is currently awaiting (resolve it to None), so a
        second menu opened concurrently doesn't share the first's live nav state."""
        pid = self._active_prompt.get(window)
        fut = self.pending_callbacks.get(pid) if pid else None
        if fut is not None and not fut.done():
            fut.set_result(None)

    @contextlib.asynccontextmanager
    async def _menu_frame(self, window, title):
        """Push one screen onto this window's navigation stack for the duration; pop on
        exit. A fresh session (a true root, or a concurrent one opened while another is
        awaiting a tap) supersedes any live prompt and gets its own list + owner token, so
        the superseded session cleans up only its own state, never the newcomer's."""
        fresh = self._active_prompt.get(window) is not None or not self._nav.get(window)
        if fresh:
            self._supersede_prompt(window)
            self._nav[window] = []  # a new list; a superseded session keeps its own reference
            self._menu_msg[window] = None
            token = object()
            self._menu_owner[window] = token
        else:
            token = self._menu_owner.get(window)
        stack = self._nav[window]
        stack.append(title)
        self._nav_pushes[window] = self._nav_pushes.get(window, 0) + 1
        try:
            yield
        finally:
            stack.pop()
            if not stack and self._menu_owner.get(window) is token:
                await self._strip_markup(window, self._menu_msg.get(window))
                self._menu_msg.pop(window, None)
                self._nav.pop(window, None)
                self._nav_pushes.pop(window, None)
                self._menu_owner.pop(window, None)

    async def _screen(self, window, title, options) -> object:
        """Render the top screen on this window's shared menu message (fresh the first
        time), adding a Back button when the stack is deeper than its root. Returns
        the chosen option value, MENU_BACK, or None (dismiss)."""
        opts = list(options)
        if len(self._nav.get(window, [])) > 1:
            opts.append(("⬅ Back", MENU_BACK))
        choice, msg_id = await self._prompt_tap(window, title, opts, self._menu_msg.get(window))
        self._menu_msg[window] = msg_id
        return choice

    async def menu(self, window, title, options) -> object:
        """Show a single-choice menu; return the chosen value (None if backed out or
        dismissed). Nested automatically: opened while a menu is already live in this
        window, it renders as a submenu with Back to the parent (the nav-stack model —
        a command that opens a menu becomes a submenu)."""
        async with self._menu_frame(window, title):
            choice = await self._screen(window, title, options)
            return None if choice == MENU_BACK else choice

    async def show_menu(self, window: str) -> None:
        """Drive the context menu as the root of the navigation stack. A tapped command
        that opens its own menu (via ctx.menu) pushes a child screen — Back returns
        here. An inline command shows its output as a child screen with Back. A plain
        one-off marks this screen triggered and dismisses the stack."""
        async with self._menu_frame(window, "menu"):
            while True:
                options = self._menu_options(window)
                label_of = {value: label for label, value in options}
                choice = await self._screen(window, self._menu_header(window), options)
                if choice is None or choice == MENU_BACK:
                    return  # dismissed, or backed out of the root
                mid = self._menu_msg[window]
                kind, _, val = choice.partition(":")
                if kind == "open":  # a launcher takes over → mark triggered, dismiss
                    await self.edit_text(window, mid, ui.triggered(label_of[choice]))
                    await self._route_launch(window, val, "", self.owner_of(window))
                    return
                if self._is_inline(choice, window):
                    body = await self._run_choice(window, kind, val)
                    text = body if isinstance(body, str) and body.strip() else "done"
                    async with self._menu_frame(window, "result"):
                        back = await self._screen(window, text, [])
                    if back is None:  # dismissed on the result screen
                        return
                    continue  # Back → re-render the menu
                # a command: it may open its own submenu(s) via ctx.menu. If it pushed a
                # screen we stay here (Back from the submenu landed back on us); if it
                # only acted, mark this screen triggered and dismiss.
                before = self._nav_pushes.get(window, 0)
                await self._run_choice(window, kind, val)
                if self._nav_pushes.get(window, 0) > before:
                    continue
                await self.edit_text(window, mid, ui.triggered(label_of[choice]))
                return

    async def _strip_markup(self, window: str, msg_id) -> None:
        if msg_id is not None:
            await self.transport_for(window).set_buttons(window, msg_id, None)

    async def _run_choice(self, window, kind, val):
        """Run a tapped menu entry; return the handler's result (a str body for an
        inline command, else None)."""
        if kind == "cmd":
            return await self._run_universal(window, val)
        if kind == "ccmd":
            return await self._run_class_command(window, val)
        return None

    async def _run_universal(self, window: str, name: str):
        entry = self.registry.universals.get(name)
        if entry is None:
            return None
        pid, attr, _help, _icon, _menu, _inline = entry
        instance = self.instances.get(window)
        if not self._allowed(instance, "universal", name):
            return None
        ctx = self._ctx(pid, None, window, name, "", instance)
        self._bump(pid, name)
        return await getattr(self.plugin_by_id[pid], attr)(ctx)

    async def _run_class_command(self, window: str, name: str):
        instance = self.instances.get(window)
        cls_id = self.owner_of(window)
        entry = self.registry.classes.get(cls_id) if cls_id else None
        if instance is None or entry is None or name not in entry.commands:
            return None
        if not self._allowed(instance, "class", name):
            return None
        attr, _help, _icon, _inline = entry.commands[name]
        ctx = self._ctx(entry.plugin_id, None, window, name, "", instance)
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

    async def startup(self) -> list[Transport]:
        """Connect each platform, revive the windows of those that connected and announce
        the bot online. Returns the platforms that failed to connect: logged and left out
        until `rejoin`."""
        live, failed = [], []
        for tx in self.transports:
            try:
                tx.bot_name = await tx.adapter.identity()
            except Exception:
                LOGGER.exception("%s failed to start; serving without it", tx.adapter.platform)
                failed.append(tx)
                continue
            live.append(tx)
        self.transports = live
        # rebuild live windows from persisted ownership (a window of a platform not
        # connected now keeps its state and revives when that platform is back)
        self.owners = dict(self.saved.get("owners", {}))
        for tx in live:
            await self._revive(tx)
        await self._announce_online()
        self._reconcile_task = asyncio.create_task(self._reconcile_loop())
        return failed

    async def rejoin(self, tx: Transport, bot_name: str) -> None:
        """A platform that failed at startup connected: serve it and revive its windows."""
        tx.bot_name = bot_name
        self.transports.append(tx)
        await self._revive(tx)

    async def _revive(self, tx: Transport) -> None:
        """Bring back every owned window of `tx`'s platform from its saved state."""
        names = self._names()
        for window, cls_id in list(self.owners.items()):
            cls = self._class_by_id(cls_id)
            if cls is None or self.find_transport(window) is not tx:
                continue
            inst = self._instantiate(cls, window, names.get(window, cls_id))
            try:
                await inst.on_revive()
            except Exception:
                LOGGER.exception("on_revive failed for %s (%s)", window, cls_id)

    async def _announce_online(self) -> None:
        """A readiness ping in each bound platform's General on every startup (the lobby
        heartbeat), plus a ping in the window that asked for the restart, if one recorded
        itself."""
        for tx in self.transports:
            if tx.adapter.owner_id is not None:
                await tx.send(tx.adapter.general, f"🟢 @{tx.bot_name} online")
        from tgforge.base.service import pop_restart_announce

        window = pop_restart_announce(self.config.home_path)
        tx = self.find_transport(window) if window is not None else None
        if tx is not None:
            await tx.send(window, "🟢 back online after a restart")
            inst = self.instances.get(window)
            if inst is not None:
                try:
                    await inst.on_restarted()
                except Exception:
                    LOGGER.exception("on_restarted failed for %s", window)

    async def broadcast_shutdown(self) -> None:
        """A graceful stop: flag it (drivers skip crash warnings) and let every live
        window settle its UI before the process exits."""
        self.shutting_down = True
        for inst in list(self.instances.values()):
            try:
                await inst.on_shutdown()
            except Exception:
                LOGGER.exception("on_shutdown failed (%s)", inst.window)
        try:
            await asyncio.wait_for(self.flush_frames(), SHUTDOWN_FLUSH)
        except TimeoutError:
            LOGGER.warning("shutdown: frames still pending after %ss", SHUTDOWN_FLUSH)

    def _class_by_id(self, cls_id: str) -> type[Topic] | None:
        entry = self.registry.classes.get(cls_id)
        return entry.cls if entry else None

    # ── Reconcile sweep (manual-deletion cleanup) ──────────────────

    async def _reconcile_loop(self) -> None:
        while True:
            await asyncio.sleep(RECONCILE_INTERVAL)
            for window in list(self.owners):
                if self.find_transport(window) is None:
                    continue
                if not await self._window_alive(window):
                    await self._drop_deleted(window)

    async def _window_alive(self, window: str) -> bool:
        """Probe a window through its adapter; only the adapter's explicit "gone" drops
        it — an error is not treated as deletion."""
        tx = self.transport_for(window)
        await tx._wait_flood()  # no call inside a flood-wait
        try:
            return await tx.adapter.window_alive(window, self._names().get(window))
        except Exception as e:
            if tx.adapter.classify(e) is None:
                raise
            return True

    async def _drop_deleted(self, window: str) -> None:
        inst = self.instances.get(window)
        if inst is not None:
            try:
                await self._teardown(inst)
            except Exception:
                LOGGER.exception("on_close failed on delete (%s)", window)
        self.db.drop(window_ns(self.owners.get(window, "core"), window))
        self._forget(window)

    # ── Routing ────────────────────────────────────────────────────

    def _ctx(self, plugin_id, message, window, text, args, instance):
        user_id = message.user_id if message is not None else None
        return Context(
            self,
            window,
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
            await ctx.send(result)  # through the window, like any reply
        return True

    async def handle_message(self, message: Inbound) -> None:
        window = message.window
        tx = self.find_transport(window)
        if tx is None:
            return
        if not message.in_thread:
            tx.note_message(window, message.message_id)
        text = message.text.strip()
        has_media = bool(message.attachments)
        mentioned = message.mentioned
        if not text and not has_media and not mentioned:
            return
        user_id = message.user_id
        if user_id is None:
            return

        if tx.adapter.owner_id is None:
            if mentioned and text.lower().startswith("/init"):
                await tx.adapter.bind_owner(user_id)
                await tx.send(window, f"@{tx.bot_name} initialized")
            return
        in_general = window == tx.adapter.general
        if not in_general and window not in self.owners:
            # a window this bot doesn't own: when bots share one group, the non-owner must
            # bow out here or it replies with help noise.
            return
        if message.echo:  # a command the platform did not show: post it first
            echo = f"› {text}"
            message.message_id = await self._in_window(window, lambda: tx.send(window, echo))

        # 1. a pending prompt (await_next / ask_text) wins first
        pending = self._await_input.get(window)
        if pending is not None:
            if has_media and not text:
                return  # a media-only message must not feed "" and cancel a text prompt
            del self._await_input[window]
            await pending(text)
            return

        if in_general and not mentioned:
            return  # General acts only on a mention

        instance = self.instances.get(window)
        cls_id = self.owner_of(window)

        if mentioned and not text and not has_media:
            await self.show_menu(window)  # a bare mention → summon the menu
            return

        # a window may take slashes as raw input (a shell): route them as text, not
        # commands. A mentioned slash still runs as a command (an explicit escape).
        raw_slash = instance is not None and not instance.slash_commands and not mentioned
        if text.startswith("/") and not raw_slash:
            await self._route_slash(message, window, text, instance, cls_id)
        else:
            await self._route_text(message, window, text, instance, cls_id)

    async def route_as_user(self, window: str, text: str, reply_to=None) -> None:
        """Replay `text` through the router as if the owner typed it in `window`,
        so a source other than a keystroke (a tapped suggestion) still hits slash/
        prefix routing — a `!cmd` button runs a shell, not an agent prompt.
        `reply_to` is the message id the replayed turn should reply to (the visible
        ack of a tapped suggestion)."""
        message = Inbound(
            window=window, message_id=reply_to, user_id=self.owner_for(window), text=text
        )
        await self.handle_message(message)

    def _split(self, text: str) -> tuple[str, str]:
        parts = text.split(maxsplit=1)
        cmd = parts[0].lower()
        args = parts[1].strip() if len(parts) > 1 else ""
        return cmd, args

    async def _route_slash(self, message, window, text, instance, cls_id):
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
                ctx = self._ctx(entry.plugin_id, message, window, text, args, instance)
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
                ctx = self._ctx(pid, message, window, text, args, instance)
                self._bump(pid, uname)
                if await self._run_command(getattr(plugin, attr), ctx, inline):
                    return

        # launch command (new window / transform / ignored, by context)
        lname = self.registry.resolve_launch(cmd)
        if lname and not _skip():
            if await self._route_launch(window, lname, args, cls_id):
                return

        # on_message (sees slashes) then on_unknown, then the General-only fallback
        if await self._route_catch(message, window, text, instance, cls_id, args):
            return
        await self._unhandled(window)

    async def _route_launch(self, window, cmd, args, cls_id) -> bool:
        cls = self.registry.launch_class[cmd]
        self._bump("core", cmd)
        if self.is_general(window):  # General → always a new window
            await self.open_window(cls, args or None, origin=window)
            return True
        if cls_id == "core":  # a core window → new or transform (core class: always new)
            if cls.id == "core":
                await self.open_window(cls, args or None, origin=window)
                return True
            choice, _ = await self.ask_buttons(
                window,
                f"Open {cls.id}:",
                [("New window", "new"), ("Transform this window", "transform")],
                timeout=60,
                announce=lambda label: f"🪄 {label}",
            )
            if choice == "new":
                await self.open_window(cls, args or None, origin=window)
            elif choice == "transform":
                await self.adopt(window, cls)
            return True
        return False  # any other window ignores the launch (falls through)

    async def _route_catch(self, message, window, text, instance, cls_id, args) -> bool:
        if cls_id and cls_id in self.registry.classes and instance is not None:
            entry = self.registry.classes[cls_id]
            for attr in (entry.on_message, entry.on_unknown):
                if attr is None:
                    continue
                ctx = self._ctx(entry.plugin_id, message, window, text, args, instance)
                if await self._run(getattr(instance, attr), ctx):
                    return True
        return False

    async def _route_text(self, message, window, text, instance, cls_id):
        # prefixes (universal, e.g. claude's ! / !!)
        for char, (pid, attr, _help) in self._sorted_prefixes():
            if text.startswith(char):
                plugin = self.plugin_by_id[pid]
                allowed = instance is None or instance.allow_universal
                if allowed:
                    ctx = self._ctx(pid, message, window, text, text, instance)
                    self._bump(pid, char)
                    if await self._run(getattr(plugin, attr), ctx):
                        return
        if await self._route_catch(message, window, text, instance, cls_id, text):
            return
        await self._unhandled(window)

    async def _unhandled(self, window: str) -> None:
        """Input nothing consumed → the help text (which points at `/` for the menu).
        A plain reply, never the interactive menu: a stray keystroke should not pop a
        keyboard, and going silent leaves the user stuck."""
        text = self.help_text(window)
        tx = self.transport_for(window)
        await self._in_window(window, lambda: tx.send(window, text))

    def _sorted_prefixes(self):
        # longer prefixes first so `!!` beats `!`
        return sorted(self.registry.prefixes.items(), key=lambda kv: -len(kv[0]))

    # ── Taps ───────────────────────────────────────────────────────

    async def handle_tap(self, tap: Tap) -> None:
        data = tap.data or ""
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
                await self._route_action(tap, act[0], act[1])
        tx = self.find_transport(tap.window)
        if tx is not None:  # a tap answer posts no message
            await tx._call(lambda: tx.adapter.answer_tap(tap), paced=False)

    async def _route_action(self, tap: Tap, name, arg) -> None:
        window = tap.window
        instance = self.instances.get(window)
        cls_id = self.owner_of(window)
        if instance is None or not cls_id:
            return
        entry = self.registry.classes.get(cls_id)
        attr = entry.actions.get(name) if entry else None
        if attr is None:
            return
        # the tapped message, acted on by the tapping user
        message = Inbound(window=window, message_id=tap.message_id, user_id=tap.user_id, text="")
        ctx = self._ctx(entry.plugin_id, message, window, "", arg, instance)
        await getattr(instance, attr)(ctx, arg)
