"""AgentTopic + AgentPlugin: the backend-neutral agent window.

Each agent window is one `AgentTopic` subclass instance. The core
here owns the turn lifecycle (send order, live card, finalize and settle, the message
buffer, bot notices, the background-jobs panel, the transcript mirror) and every shared
command; a backend subclass drives its CLI through the backend boundary (`backend_*`)
and turns the CLI's output into the neutral events of `session.py`. The per-window
session state persists in the window store; account/model/workspace config is the
plugin's, in the plugin store.
"""

from __future__ import annotations

import asyncio
import codecs
import contextlib
import dataclasses
import glob as _glob
import logging
import os
import random
import re
import time
import uuid
from pathlib import Path

from tgforge.base import ui
from tgforge.base.kernel import (
    NOTICE_EXCEPTION,
    NOTICE_FALLBACK,
    NOTICE_NOT_DELIVERED,
    SHUTDOWN_FLUSH,
    STALL_WARN,
    AppDB,
    Notice,
    Plugin,
    SavedDict,
    Topic,
    action,
    command,
    error_text,
    notice_exception,
    notice_line,
    on_message,
    on_unknown,
    plugin_ns,
    prefix,
    reap,
    service,
    shared_ns,
    universal,
)
from tgforge.base.service import service_manager
from tgforge.base.ui import (
    MAX_MSG,
    Pages,
    code_block,
    expandable_pages,
    fmt_duration,
    shell_md,
    shell_view,
)
from tgforge.plugins.agent import background
from tgforge.plugins.agent.render import (
    WORDS,
    context_part,
    fmt_tokens,
    quota_part,
    render_timeline,
    status_head,
)
from tgforge.plugins.agent.session import (
    TURN_EVENTS,
    JobEnded,
    JobStarted,
    PromptFolded,
    Title,
    TurnState,
    Usage,
    apply,
    finalize_answer,
)

LOGGER = logging.getLogger("tgforge")

SEND_STEP_STALL = 60.0  # s a send step waits on the one before it before a WARNING
EDIT_INTERVAL = 2.5  # seconds between live-card frames (the kernel skips what it can't show)
MESSAGE_DEBOUNCE = 1.0  # quiet seconds before buffered messages go out as one prompt
RELEASE_IDLE_SEC = 1800
UPDATER_INTERVAL = 5.0
MIRROR_INTERVAL = 3.0
SHELL_TIMEOUT = 120
SHELL_KEEP_CHARS = 2 * MAX_MSG  # the ! output tail kept (the view shows a MAX_MSG tail)
SHELL_READ_SIZE = 65536  # bytes per read of the ! output pipe
PANEL_FRAME = "background"  # the bottom frame holding the background-jobs panel
UNDELIVERED_NOTE = (
    "⚠️ not delivered — the session process exited before reading this message. Send it again."
)
# what `_end_turn` decided for the process
TURN_QUEUED = "queued"  # a queued message runs next: the process stays
TURN_KEPT = "kept"  # idle with background jobs: kept until RELEASE_IDLE_SEC
TURN_IDLE = "idle"  # idle, nothing running: the backend ends the process


# the session note's rules; `{…}` fields are the window's platform and its limits
SESSION_BRIEF = (
    "Interactive tools (AskUserQuestion) are unavailable — "
    "when a decision is needed, state it plainly and stop. Always end your message "
    "with one line `[[suggest]] reply one | reply two | reply three` predicting the "
    "user's three most probable next replies — the bot turns each into a button "
    "that sends that text as the user's next message. Files the user attaches or "
    "forwards (any type, photos included) are downloaded locally and their paths "
    "listed in the prompt's [Attached file(s)] block; if the user mentions a sent "
    "file with no path, look for the newest matching file there ({download_cap} "
    "{platform} download cap). To send the user a file, end your message with one line "
    "per file: `[[attach]] /absolute/path` — sent after the text as a photo (images "
    "≤{photo_cap}) or a document (≤{upload_cap}); the marker line never renders. "
    "{table_rule}Keep lists one level deep — no nested bullets; group detail into short "
    "paragraphs led by a bold label instead. The bot reports trouble with its own "
    "messages in a [Bot notice(s)] block, one line each (kind: what it hit, the form "
    "tried → the form used, the error): `not delivered` — that message, or the cut tail "
    "of it, never reached the user, so send it again; `exception` — the bot itself "
    "failed, so tell the user what failed; `fallback` — it went out in a plainer form, "
    "no action."
)
TABLE_RULE = (
    "Do not use Markdown tables in replies — {platform} renders them as raw text, not a "
    "grid; present tabular data as short labeled lines or a bullet list instead. "
)


def _size_cap(size: int) -> str:
    return f"{size // (1024 * 1024 * 1024)}GB" if size >= 1024**3 else f"{size // (1024 * 1024)}MB"


def session_brief(adapter) -> str:
    """`SESSION_BRIEF` filled in with `adapter`'s platform name and limits."""
    table_rule = "" if adapter.renders_tables else TABLE_RULE.format(platform=adapter.platform)
    return SESSION_BRIEF.format(
        platform=adapter.platform,
        download_cap=_size_cap(adapter.download_max),
        photo_cap=_size_cap(adapter.photo_max),
        upload_cap=_size_cap(adapter.upload_max),
        table_rule=table_rule,
    )


ATTACHED_HEAD = "[Attached file(s), saved locally — Read to view:"


def attached_files(prompt: str) -> list[Path]:
    """The saved attachments a prompt's [Attached file(s)] block lists."""
    head = prompt.find(ATTACHED_HEAD + "\n")
    if head < 0:
        return []
    body = prompt[head + len(ATTACHED_HEAD) + 1 :]
    end = body.find("\n]")
    return [Path(line) for line in body[: end if end >= 0 else len(body)].splitlines() if line]


def system_prompt_file(workspace: Path) -> Path | None:
    """The nearest system_prompt.txt at or above the workspace — the same lookup
    the interactive shell wrapper does, so bot turns and CLI sessions run under one
    system prompt."""
    for directory in (workspace, *workspace.parents):
        candidate = directory / "system_prompt.txt"
        if candidate.is_file():
            return candidate
    return None


APP_MODELS = "app_models"  # the plugin-store key holding the app's own model list
WINDOW_SETTINGS = ("model", "effort")  # a window's settings `tgforge window` also writes
SETTING_ICONS = {"model": "🧠", "effort": "⚡"}


def write_window_setting(store: SavedDict, name: str, value: str | None) -> None:
    """Write one window setting's own key (None: the default, no key)."""
    if value is None:
        store.pop(name, None)
    else:
        store[name] = value


def setting_note(name: str, value: str | None) -> str:
    """The note a model or effort switch posts in its window: `🧠 model → <value>`."""
    return f"{SETTING_ICONS[name]} {name} → {value or 'default'}"


ROOTS_STORE = "agent"  # the shared store holding the workspace roots (`roots`)
LEGACY_ROOTS_NS = plugin_ns("claude")  # where the roots lived before the shared store


def move_legacy_roots(store: SavedDict) -> None:
    """Move roots saved under the old Claude-plugin key into the shared store, once."""
    store.move_from(LEGACY_ROOTS_NS, "roots")


def roots_store(home: Path) -> SavedDict:
    """The workspace roots' store in a bot home's state.db — the same store the running
    bot's agent plugins read, so offline edits (install, `tgforge workspaces`) and
    runtime stay in sync."""
    store = AppDB(home / "state.db").saved(shared_ns(ROOTS_STORE))
    move_legacy_roots(store)
    return store


def read_roots(home: Path) -> list[str]:
    return list(roots_store(home).get("roots") or [])


def write_roots(home: Path, roots: list[str]) -> None:
    roots_store(home)["roots"] = list(roots)


def resolve_roots(globs: list[str]) -> list[Path]:
    """Expand workspace-root globs/paths to existing dirs; order preserved, deduped."""
    out: list[Path] = []
    seen: set[Path] = set()
    for pattern in globs:
        p = str(Path(pattern).expanduser())
        matches = _glob.glob(p) if any(c in p for c in "*?[") else [p]
        for m in sorted(matches):
            path = Path(m)
            if path.is_dir():
                rp = path.resolve()
                if rp not in seen:
                    seen.add(rp)
                    out.append(rp)
    return out


class AgentTopic(Topic):
    """The neutral agent window. A backend subclass sets the class attributes below and
    implements the backend boundary (`backend_*`) and the session-file hooks."""

    agent_name = "agent"  # the agent's name in user-facing text
    help_fields = {"agent": agent_name}  # `{agent}` in the command help

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        cls.help_fields = {"agent": cls.agent_name}  # one value per backend

    PERMISSION_MODES: tuple[str, ...] = ()  # the `/mode` choices
    EFFORTS: tuple[str, ...] = ("low", "medium", "high")  # the `/effort` choices

    def __init__(self, core, window, name, saved):
        super().__init__(core, window, name, saved)
        # persisted (window store)
        self.session_id: str | None = self._new_session_id()
        # the effective model and effort (read from the window store at each use) as the
        # window last noted them in its thread and as its backend runs them; a value
        # written from outside differs from both until the next turn
        self.noted_settings: dict[str, str | None] = {}  # filled by on_open / on_revive
        self.applied_settings: dict[str, str | None] = {}
        self.config_dir: str | None = None  # the account's data dir; None = the default one
        self.workspace = ""
        self.title: str | None = None  # the session name shown as the title suffix
        self.agent_title: str | None = None  # the newest title the agent gave the session
        self.mirror_offset = -1
        self.context_window: int | None = None  # the model's, as the backend reported it
        # runtime
        self.owned = False  # the bot drives the session now (the mirror stays quiet)
        self.busy = False
        self.turn_open = False  # a turn is running: from its open to its end
        self.lock = asyncio.Lock()
        self.heartbeat_task: asyncio.Task | None = None
        self.mirror_task: asyncio.Task | None = None
        self.send_chain: asyncio.Task | None = None  # the newest queued send step
        self.send_steps: set[asyncio.Task] = set()  # every queued send step not done
        self.last_reader_event = 0.0
        self._stall_warned = False
        self.word_seed = 0
        self.spin = 0
        self.pending_writes: list[dict] = []
        self.turn_seq = 0  # boundary counter: bumped once per turn end; stamps each ack's turn
        # bot notices held for the next prompt's [Bot notice(s)] block, keyed by kind,
        # what and error (a repeat counts up); a notice prompt sent closes immediate ones
        # until the user's next prompt, so a failing reply to it never starts another
        self.pending_notices: dict[tuple, Notice] = {}
        self.notice_prompt_open = False
        self.notice_task: asyncio.Task | None = None
        self.cancel_requested = False
        self.cancel_msg_id: int | None = None  # the "cancelling…" bubble, settled at turn end
        self.turn = TurnState()
        self.release_task: asyncio.Task | None = None
        self.background_tasks: dict[str, dict] = {}
        self.background_labels: dict[str, str] = {}  # this turn's tool call id → job label
        self.background_updater_task: asyncio.Task | None = None
        self.mirror_holder: int | None = None
        self.mirror_holder_queued = False  # the "working…" send is queued, not sent yet
        self.mirror_tools = 0
        self.mirror_recent: list[str] = []
        self.mirror_start = 0.0
        self.mirror_tick = 0
        self._suggested: dict[str, dict] = {}
        self.message_buffer: list[tuple] = []  # (Inbound, text, download task)
        self.message_last_arrival = 0.0
        self.message_flush_task: asyncio.Task | None = None

    # ── The backend boundary (implemented by each backend) ─────────
    def backend_alive(self) -> bool:
        """Whether the CLI process runs."""
        raise NotImplementedError

    async def backend_start(self) -> None:
        """Make sure the CLI process runs this window's session (`session_id`; a fresh
        one when it has none or none exists yet, else resumed); raises on failure."""
        raise NotImplementedError

    async def backend_prompt(self, text: str) -> str | None:
        """Send a prompt to the running session; returns its prompt id (the id
        `PromptFolded` names it by), or None when the backend has none. The
        saved attachments it lists: `attached_files(text)`."""
        raise NotImplementedError

    async def backend_cancel(self) -> bool:
        """Ask the CLI to stop the running turn; whether the request went out."""
        raise NotImplementedError

    async def backend_cancelled(self) -> None:
        """The cancel request and its "cancelling..." note are out: a backend whose CLI
        sends no end for the running turn ends it here."""

    async def backend_set_option(self, name: str, value: str | None) -> None:
        """A per-window `model` / `effort` or the app-wide `mode` changed."""
        raise NotImplementedError

    async def backend_stop_job(self, job_id: str, task: dict) -> None:
        """Stop the running background job `job_id` (`task` its row), reporting the
        outcome in the window."""
        raise NotImplementedError

    def backend_end(self) -> None:
        """End the CLI process (the session stays resumable)."""
        raise NotImplementedError

    async def backend_close(self) -> None:
        """The window closes: stop reading and reap the CLI process."""
        raise NotImplementedError

    # ── Session-file hooks (implemented by each backend) ───────────
    def _new_session_id(self) -> str | None:
        """The id a fresh session starts with; None when the CLI assigns it."""
        return str(uuid.uuid4())

    def _transcript_path(self) -> Path | None:
        """The session's transcript file, mirrored while the bot does not drive it."""
        raise NotImplementedError

    def _mirror_parse(self, raw: str) -> tuple[list[str], list[str], list[tuple[str, str]], bool]:
        """New transcript lines → (messages, tool lines, images, whether mid-turn)."""
        raise NotImplementedError

    def _write_title(self, title: str) -> None:
        """Record a user-given title with the session, where the CLI keeps one."""

    async def _generate_title(self) -> str | None:
        """A title for the session, for `/rename` with no name: the newest title the
        agent gave the session (`Title`), if any."""
        return self.agent_title

    def _carry_transcript(self, old_dir: Path, new_dir: Path) -> None:
        """Copy the session's files from one account's data dir to another's."""
        raise NotImplementedError

    def _cli_line(self) -> str:
        """The shell line that resumes this session on the PC."""
        raise NotImplementedError

    def _account_key(self) -> str:
        return str(self._session_config_dir())

    def _session_config_dir(self) -> Path:
        return Path(self.config_dir) if self.config_dir else self.plugin.default_account_dir

    def _quota(self) -> dict | None:
        """The quota the status line shows: the account's newest one."""
        return self.plugin.quota.get(self._account_key())

    def _held_jobs(self) -> list[str]:
        """The labels of the running jobs that ending this window's process would take
        down: the window then never ends it on its own initiative. None by default (a
        backend whose jobs outlive their CLI)."""
        return []

    def _running_mode(self) -> str:
        """The permission mode this window's process runs in: the app's, unless the
        backend holds a process started under another one."""
        return self.plugin.permission_mode

    async def _refused_for_jobs(self, what: str) -> bool:
        """Refuse `what` (a fresh session, a switch) while the process runs a job."""
        held = self._held_jobs()
        if held:
            names = ", ".join(held)
            await self.send(
                f"a background job runs ({names}) — /kill it or wait for it, then {what}"
            )
        return bool(held)

    def _probe_jobs(self) -> None:
        """Find background jobs that ended without an event saying so (the updater's tick)."""

    def _account_name(self, config_dir: Path) -> str:
        return self.plugin.account_name(config_dir)

    # ── Convenience ────────────────────────────────────────────────
    def _env_tag(self) -> str:
        """The `[via <platform> bot · …]` locator leading the session note: which
        bot on which platform, its process manager, and where it runs. Absent fields are
        dropped."""
        cfg = self._core.config
        name = (cfg.service or self.adapter.bot_name or "tgforge").strip()
        parts = [
            f"via {self.adapter.platform} bot · {name[:1].upper() + name[1:]}",
            service_manager(),
        ]
        if cfg.service:
            parts.append(f"service `{cfg.service}`")
        parts.append(f"home {cfg.home}")
        return "[" + " · ".join(parts) + "]"

    def _app_brief(self) -> str:
        """App-supplied guidance appended after the generic brief: the plugin's
        `brief=` if set, else a `session_brief.md` in the bot home. Resolved once."""
        p = self.plugin
        if p.brief_resolved is None:
            if p.brief is not None:
                p.brief_resolved = p.brief
            else:
                try:
                    p.brief_resolved = (
                        (self._core.config.home_path / "session_brief.md").read_text().strip()
                    )
                except OSError:
                    p.brief_resolved = ""
        return p.brief_resolved

    def _session_note(self) -> str:
        """The session note — env tag, `SESSION_BRIEF`, app brief — every turn of the
        CLI process runs under (a slash skill too)."""
        extra = self._app_brief()
        brief = session_brief(self.adapter)
        return f"{self._env_tag()} {brief}" + (f"\n{extra}" if extra else "")

    def _session_note_file(self) -> Path:
        """This window's note file in the bot home (a file, not an argument: a long app
        brief would pass the kernel's argument cap)."""
        return self._core.config.home_path / "session_notes" / f"{self.window}.md"

    def _write_session_note(self) -> Path:
        path = self._session_note_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self._session_note())
        return path

    def _default_workspace(self) -> str:
        ws = self.plugin.workspaces
        return str(ws[0]) if ws else str(self._core.config.home_path)

    # ── Window settings: the window store's own keys, read at each use ──
    @property
    def model(self) -> str | None:
        return self.saved.get("model")

    @model.setter
    def model(self, value: str | None) -> None:
        self._write_setting("model", value)

    @property
    def effort(self) -> str | None:
        return self.saved.get("effort")

    @effort.setter
    def effort(self, value: str | None) -> None:
        self._write_setting("effort", value)

    def _write_setting(self, name: str, value: str | None) -> None:
        """Write one window setting's own key (None: the default); the window's own
        switch posts its note itself."""
        write_window_setting(self.saved, name, value)
        self.noted_settings[name] = self._effective_setting(name)

    def _effective_setting(self, name: str) -> str | None:
        """The model or effort the next process runs: the window's own, else the
        plugin's default model (None: the CLI's own default)."""
        return (self._model_id() or None) if name == "model" else self.effort

    def _setting_note(self, name: str) -> str:
        """`🧠 model → <value>`; a window on the default names the default model."""
        value = self.saved.get(name)
        effective = self._effective_setting(name)
        if value is None and name == "model" and effective:
            return f"{setting_note(name, None)} ({effective})"
        return setting_note(name, value)

    def _settings_now(self) -> dict[str, str | None]:
        return {name: self._effective_setting(name) for name in WINDOW_SETTINGS}

    async def _apply_settings(self) -> None:
        """Bring an idle backend to the window's model and effort (after `/model`,
        `/effort` or a write from outside); a running turn keeps its settings, so the
        next turn applies them."""
        if self.busy:
            return
        for name in WINDOW_SETTINGS:
            value = self._effective_setting(name)
            if name in self.applied_settings and value == self.applied_settings[name]:
                continue
            self.applied_settings[name] = value
            self._save()
            await self.backend_set_option(name, value)

    async def _sync_settings(self) -> None:
        """Before a turn: note a model or effort the window has not noted yet (written
        by `tgforge window`, or a new plugin default model for a window on the
        default) the way `/model` / `/effort` do, then apply it."""
        for name in WINDOW_SETTINGS:
            value = self._effective_setting(name)
            if name not in self.noted_settings:  # nothing noted yet: nothing changed
                self.noted_settings[name] = value
            if value == self.noted_settings[name]:
                continue
            self.noted_settings[name] = value
            if name == "model":
                self.context_window = None  # the backend reports the new model's window
            self._save()
            note = self._setting_note(name)
            self._send_in_order(lambda note=note: self.send(note))
        await self._apply_settings()

    # ── Lifecycle ──────────────────────────────────────────────────
    async def on_open(self):
        self.workspace = self._default_workspace()
        self._save()
        await self.send(
            "session ready\n\ntype a message → agent prompt\n"
            "/command → agent skill · /stop closes · /cancel stops a turn"
        )
        await self._pick_workspace()
        await self._pick_account(allow_new=False)
        await self._pick_model_effort()
        self.noted_settings, self.applied_settings = self._settings_now(), self._settings_now()
        self._save()
        self._start_mirror()

    async def on_revive(self):
        data = self.saved.get("session", {})
        self.session_id = data.get("session_id", self.session_id)
        for name in WINDOW_SETTINGS:  # saved inside the session record before: moved once
            if name in data and name not in self.saved:
                write_window_setting(self.saved, name, data[name])
        now = self._settings_now()  # a record from before noted and applied what it held
        self.noted_settings = {**now, **data.get("noted", {})}
        self.applied_settings = {**now, **data.get("applied", {})}
        self.config_dir = data.get("config_dir")
        self.workspace = data.get("workspace") or self._default_workspace()
        self.title = data.get("title")
        self.context_window = data.get("context_window")
        self.mirror_offset = data.get("mirror_offset", -1)
        self.mirror_holder = data.get("mirror_holder")
        self.mirror_tools = data.get("mirror_tools", 0)
        self.mirror_start = data.get("mirror_start", 0.0)
        self._save()  # the record without the moved settings, so a reset stays reset
        self._start_mirror()
        held = self.saved.get("notices", [])
        if held:
            del self.saved["notices"]
            for fields in held:  # raised while the bot stopped: delivered now
                self.on_notice(Notice(**fields))

    async def on_close(self):
        for t in (
            self.heartbeat_task,
            self.mirror_task,
            self.background_updater_task,
            self.release_task,
            self.message_flush_task,
            *self.send_steps,
        ):
            if t is not None:
                t.cancel()
        await self.backend_close()  # stop the reader, reap the CLI child so it never leaks
        self._session_note_file().unlink(missing_ok=True)

    def _save(self):
        self.saved["session"] = {
            **({"session_id": self.session_id} if self.session_id else {}),
            "workspace": self.workspace,
            "mirror_offset": self.mirror_offset,
            **({"title": self.title} if self.title else {}),
            **({"config_dir": self.config_dir} if self.config_dir else {}),
            **({"context_window": self.context_window} if self.context_window else {}),
            "noted": self.noted_settings,
            "applied": self.applied_settings,
            **(
                {
                    "mirror_holder": self.mirror_holder,
                    "mirror_tools": self.mirror_tools,
                    "mirror_start": self.mirror_start,
                }
                if self.mirror_holder is not None
                else {}
            ),
        }

    def _start_mirror(self):
        if self.mirror_task is None or self.mirror_task.done():
            self.mirror_task = asyncio.create_task(self._mirror_loop())

    # ── Turn driving ───────────────────────────────────────────────
    def _arm_heartbeat(self) -> bool:
        """Start the live-card painter if it isn't running; return whether it was
        (re)armed. Idempotent — the singleton guarantee — so every turn-open path can
        arm without risking a second painter. Armed at the turn-open boundary
        (`_open_holder`), the one point all entry paths share, not at process start:
        a turn triggered by a background-job completion reuses a live process and
        never starts one, so arming there left its card frozen at frame one."""
        if self.heartbeat_task is None or self.heartbeat_task.done():
            self.heartbeat_task = asyncio.create_task(self._heartbeat())
            return True
        return False

    def _send_in_order(self, step) -> asyncio.Task:
        """Run `step` (a zero-arg coroutine factory that sends/deletes this window's
        messages) after every step queued before it, as its own task, and return the
        task. Every message of the window goes through here (directly, or via the
        `send`/`delete` family and `run_message_call`), so the order on screen is the order of
        the queue and no reader or loop ever waits on the platform."""
        previous = self.send_chain

        async def run():
            if previous is not None:
                done, _pending = await asyncio.wait([previous], timeout=SEND_STEP_STALL)
                if not done:  # a step that never ends holds every later message back
                    LOGGER.warning(
                        "send step (%s) waits over %ss on the step before it",
                        self.name,
                        SEND_STEP_STALL,
                    )
                    stalled = f"waits over {SEND_STEP_STALL:.0f}s on the step before it"
                    self._core.notice(
                        Notice(NOTICE_EXCEPTION, self.window, "send step", "", stalled)
                    )
                    await asyncio.wait([previous])
            try:
                await step()
            except Exception as error:
                LOGGER.exception("send step failed (%s)", self.name)
                notice_exception(self._core.notice, self.window, "send step", error)

        task = asyncio.create_task(run())
        self.send_chain = task
        self.send_steps.add(task)
        task.add_done_callback(self.send_steps.discard)
        return task

    async def run_message_call(self, call):
        """Every message this window posts or deletes (its `send` family, and the
        kernel's menus and prompts) runs as a send step, returning its result; directly
        when already inside one (a step never waits on a later step)."""
        if asyncio.current_task() in self.send_steps:
            return await call()
        done = asyncio.get_running_loop().create_future()

        async def step():
            try:
                done.set_result(await call())
            except Exception as exc:
                done.set_exception(exc)

        task = self._send_in_order(step)
        # a step cancelled (the window closed), even before it started, releases its caller
        task.add_done_callback(lambda _task: done.done() or done.cancel())
        return await done

    async def _sent(self) -> None:
        """Wait until every queued send step has run."""
        if self.send_chain is not None:
            await asyncio.wait([self.send_chain])

    def _open_holder(self, reply_to=None, ack=None, awaits_replay=False):
        """Open a turn: fresh turn state, then queue its live card behind the previous
        turn's output, so the card always lands below that turn's last message. `ack` is
        the queued message this turn starts from: its "queued ✓" bubble is deleted and
        the card replies to the user's message instead. `awaits_replay`: the turn is a
        user prompt's, and the CLI has not echoed that prompt yet."""
        self.turn = TurnState(
            reply_to=reply_to,
            started=time.monotonic(),
            awaits_replay=awaits_replay,
            prompt_id=ack.get("prompt_id") if ack is not None else None,
        )
        self.turn_open = True
        self.busy = True
        self.last_reader_event = self.turn.started
        self._stall_warned = False
        self.word_seed = random.randrange(len(WORDS))
        self.spin = 0
        turn = self.turn

        async def send_card():
            if ack is not None and ack.get("bubble_id") is not None:
                await self.delete(ack["bubble_id"])
            turn.card_id = await self.send(self._live_view(turn), reply_to=turn.reply_to)

        self._send_in_order(send_card)
        # arm the painter for THIS turn. Every turn-open path lands here (submit, a
        # background-completion opened by the reader, a prompt replay, the result
        # fallback); the prior turn's close let it exit.
        revived = self._arm_heartbeat()
        LOGGER.info("turn start (%s): live panel %s", self.name, "revived" if revived else "alive")

    def _open_next_turn(self) -> None:
        """Open the turn the CLI just started between turns: the oldest queued message's
        (the CLI runs its input queue in order, and a local slash command runs with no
        replay), else its own (a background completion)."""
        if self.pending_writes:
            entry = self.pending_writes.pop(0)
            self._open_holder(reply_to=entry["reply_to"], ack=entry, awaits_replay=True)
        else:
            self._open_holder(reply_to=self.turn.reply_to)

    def _reorder_holder(self) -> asyncio.Task:
        """Queue a move of the live card to the bottom: send it anew, delete the old."""
        turn = self.turn

        async def move():
            old = turn.card_id
            if old is None:
                return
            new_id = await self.send(self._live_view(turn), reply_to=turn.reply_to)
            if new_id is None:
                return
            turn.card_id = new_id
            await self.delete(old)

        return self._send_in_order(move)

    def _delete_ack(self, entry: dict) -> None:
        """Queue the delete of a folded message's "queued ✓" bubble."""

        async def delete():
            if entry.get("bubble_id") is not None:
                await self.delete(entry["bubble_id"])

        self._send_in_order(delete)

    def _flag_undelivered(self, entry: dict) -> None:
        """Queue the swap of a queued message's "queued ✓" bubble for a notice, replying
        to the message, that the process exited before running it."""
        LOGGER.warning("reader (%s): proc exited with a queued message unread", self.name)

        async def flag():
            if entry.get("bubble_id") is not None:
                await self.delete(entry["bubble_id"])
            await self.send(UNDELIVERED_NOTE, reply_to=entry["reply_to"])

        self._send_in_order(flag)

    async def submit(self, prompt, reply_to=None):
        """The single prompt entry point (on_message, a skill slash, the ! chain,
        and the agent.prompt service all funnel here)."""
        async with self.lock:
            if self.busy and not self.backend_alive():
                self.busy = False
                self.turn_open = False
                self.pending_writes = []
            try:
                if not self.busy:  # a running turn keeps its settings
                    await self._sync_settings()
                await self.backend_start()
            except Exception as exc:
                note = f"failed to launch: {exc}"  # `exc` is unbound once the block ends
                self._send_in_order(lambda: self.send(note))
                return
            LOGGER.info(
                "submit (%s): busy=%s proc_alive=%s pending=%d card=%s seq=%d :: %r",
                self.name,
                self.busy,
                self.backend_alive(),
                len(self.pending_writes),
                self.turn.card_id,
                self.turn_seq,
                prompt[:40],
            )
            if self.busy:
                entry = {
                    "is_initiator": False,
                    "bubble_id": None,
                    "reply_to": reply_to,
                    "turn_seq": self.turn_seq,
                }
                self.pending_writes.append(entry)

                async def send_ack():
                    entry["bubble_id"] = await self.send("queued ✓", reply_to=reply_to)
                    LOGGER.info(
                        "submit (%s): QUEUED (ack bubble=%s)", self.name, entry["bubble_id"]
                    )

                self._send_in_order(send_ack)
                entry["prompt_id"] = await self.backend_prompt(prompt)
            else:
                LOGGER.info("submit (%s): NEW turn (busy was False)", self.name)
                self.cancel_requested = False
                self._open_holder(reply_to=reply_to, awaits_replay=True)
                entry = {
                    "is_initiator": True,
                    "bubble_id": None,
                    "reply_to": reply_to,
                    "turn_seq": self.turn_seq,
                }
                self.pending_writes.append(entry)
                prompt_id = await self.backend_prompt(prompt)
                entry["prompt_id"] = self.turn.prompt_id = prompt_id

    async def on_restarted(self):
        """This topic asked for the restart: re-enter the session so it resumes on its
        own — whoever hit restart (a running turn or the user) is handed back in."""
        await self.submit("🔄 Bot restarted and is back online — continue where you left off.")

    async def on_shutdown(self):
        """A graceful restart: let queued output land, then settle a live turn card into a
        final bubble (drop the spinner, keep the timeline) so nothing is left mid-render;
        the session revives and re-mirrors after the restart."""
        self._save_notices()  # held for the next prompt, which comes after the restart
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._sent(), SHUTDOWN_FLUSH)
        turn = self.turn
        if not self.turn_open or turn.card_id is None:
            return
        self.turn_open = False  # the painter stops at its next tick
        await self._stop_heartbeat()
        note = "⏸ interrupted by a restart — resuming shortly"
        lines, _, _ = render_timeline(turn.timeline, drop_last_text=False)
        pages = expandable_pages(
            self._summary(turn, time.monotonic()), lines, self.adapter.page_size
        )
        shown = Pages(pages.summary, pages.pages[:1], note)
        if await self.land_frame(turn.card_id, None, shown):
            await self.send_md(shown, first_id=turn.card_id)  # Slack: page 1 in the thread

    def _warn_interrupted(self):
        """Flag a turn cut short by a dead process — but stay silent on a graceful
        bot restart, which re-adopts the session (nothing was lost, no alarm needed)."""
        card_id = self.turn.card_id
        if self.turn_open and card_id is not None and not self._core.shutting_down:
            self.land_frame(
                card_id,
                "⚠️ the session process exited mid-turn — nothing on disk was lost. "
                "Send your message again to retry, or /cli to resume on the PC.",
            )

    # ── Backend events ─────────────────────────────────────────────
    def _note_activity(self) -> None:
        """The CLI said something: the stall clock restarts, an idle release waits."""
        self.last_reader_event = time.monotonic()
        if self.release_task is not None:
            self.release_task.cancel()
            self.release_task = None

    async def on_event(self, ev) -> None:
        """One neutral event from the backend: a turn event folds into the running turn
        (opening one when none runs); a window event updates the jobs, the title, or
        the queued prompts."""
        kind = type(ev)
        if kind in TURN_EVENTS:
            if not self.turn_open:
                self._open_next_turn()
            apply(self.turn, ev)
            if kind is Usage and ev.context_size:
                self.context_window = ev.context_size
        elif kind is JobStarted:
            background.add_job(self, ev.id, ev.label, ev.path)
            self._ensure_background_updater()
        elif kind is JobEnded:
            if background.end_job(self, ev.id, ev.mark):
                self._ensure_background_updater()
        elif kind is Title:
            self.agent_title = ev.text  # for a bare `/rename`; the topic name stays
        elif kind is PromptFolded:
            self._fold_prompt(ev.prompt_id)

    def _fold_prompt(self, prompt_id: str) -> None:
        """The queued prompt `prompt_id` joined the running turn: its ack goes, the card
        moves below it, and the turn now runs under that prompt."""
        entry = next((e for e in self.pending_writes if e.get("prompt_id") == prompt_id), None)
        if entry is None or not self.turn_open:
            return
        self.pending_writes.remove(entry)
        self.turn.prompt_id = prompt_id
        self._fold_prompts([entry])

    def _fold_prompts(self, entries: list[dict]) -> None:
        """Queued prompts (taken off `pending_writes`) joined the running turn: each
        acked one's "queued ✓" goes, and the card makes one move below them."""
        folded = [e for e in entries if not e["is_initiator"]]
        for entry in folded:
            self._delete_ack(entry)
        if folded:
            self._reorder_holder()

    async def _end_turn(
        self, result_text, subtype="success", is_error=False, context_window=None, keep=False
    ) -> str:
        """The turn ended: close it (`_finalize_turn`), then decide atomically vs
        `submit()` what the process does — a message after the boundary either queued
        here (busy still True → pending_writes) or, once the process ends, respawns a
        fresh one, never writing to a process being ended. `keep`: background work was
        launched this turn. Returns TURN_QUEUED, TURN_KEPT or TURN_IDLE."""
        if not self.turn_open:
            self._open_next_turn()
        await self._finalize_turn(
            result_text, subtype=subtype, is_error=is_error, context_window=context_window
        )
        async with self.lock:
            if self.pending_writes:
                LOGGER.info(
                    "reader (%s): result done, %d queued → next turn",
                    self.name,
                    len(self.pending_writes),
                )
                self.cancel_requested = False  # the next queued turn cancels itself
                return TURN_QUEUED
            self.busy = False
            running = any(x["done"] is None for x in self.background_tasks.values())
            if keep or running:
                if self.release_task is not None:
                    self.release_task.cancel()
                self.release_task = asyncio.create_task(self._release_idle())
                return TURN_KEPT
            LOGGER.info("reader (%s): result done, idle → ending proc", self.name)
            return TURN_IDLE

    async def _process_exited(self) -> None:
        """The CLI's output ended (the process exited or the reader stopped): settle
        what the turn left — the card, a cancel note, each queued message's ack (swapped
        for UNDELIVERED_NOTE unless the window is closing) — and clear the turn state."""
        if self.release_task is not None:
            self.release_task.cancel()
            self.release_task = None
        await self._stop_heartbeat()  # own the card before warn_interrupted paints it
        self._warn_interrupted()
        await self._settle_cancel_note()  # a cancel that ended via a dead proc
        if not asyncio.current_task().cancelling():  # cancelled = the window is going
            for entry in self.pending_writes:
                if not entry["is_initiator"]:
                    self._flag_undelivered(entry)
        self._note_transcript_offset()
        self.busy = False
        self.turn_open = False
        self.pending_writes = []
        self.owned = False

    def _note_transcript_offset(self) -> None:
        """The mirror resumes after what the session's transcript holds now."""
        path = self._transcript_path()
        if path is not None and path.exists():
            self.mirror_offset = path.stat().st_size

    async def _release_idle(self):
        """End the process after RELEASE_IDLE_SEC of quiet; while it runs a job
        (`_held_jobs`) the wait starts over, so the release follows the last job's end."""
        try:
            await asyncio.sleep(RELEASE_IDLE_SEC)
            while self._held_jobs():
                await asyncio.sleep(RELEASE_IDLE_SEC)
        except asyncio.CancelledError:
            return
        self.release_task = None
        if self.backend_alive():
            self.backend_end()

    # ── Usage: context + quota ─────────────────────────────────────
    def _model_id(self) -> str:
        return self.model or self.plugin.default_model or ""

    def _usage_parts(self, context_tokens: int) -> list[str]:
        """The context and quota parts of a status or summary line."""
        parts = [
            context_part(context_tokens, self.context_window),
            quota_part(self._quota()),
        ]
        return [p for p in parts if p]

    def _summary(self, turn: TurnState, ended: float, drop_last_text: bool = False) -> str:
        """`🔧 N · 💬 N · duration · ↓ tokens · ctx P% (T) · 5h P% · 7d P%`."""
        _, n_tools, n_narr = render_timeline(turn.timeline, drop_last_text)
        parts = []
        if n_tools:
            parts.append(f"🔧 {n_tools}")
        if n_narr:
            parts.append(f"💬 {n_narr}")
        parts.append(fmt_duration(int(ended - turn.started)))
        parts.append(f"↓ {fmt_tokens(turn.tok_base + turn.tok_latest)}")
        return " · ".join(parts + self._usage_parts(turn.context_tokens))

    def _live_view(self, turn: TurnState) -> str:
        """The live card: the status line over the tail of the running timeline."""
        elapsed = int(time.monotonic() - turn.started)
        tokens = turn.tok_base + turn.tok_latest
        head = status_head(
            self.word_seed, self.spin, elapsed, tokens, self._usage_parts(turn.context_tokens)
        )
        lines, _, _ = render_timeline(turn.timeline, drop_last_text=False)
        thinking = "".join(turn.thinking_parts).strip()
        answer = "".join(turn.preview_parts).strip()
        if thinking:
            lines.append(f"🧠 {thinking}")
        if answer:
            lines.append(f"💬 {answer}")
        interim = "\n\n".join(lines)
        base = f"{head}\n\n{interim}" if interim else head
        if len(base) > MAX_MSG:  # the live card shows the tail only
            base = f"{head}\n\n…{base[-(MAX_MSG - len(head) - 5) :]}"
        return base

    # ── Background-jobs panel ──────────────────────────────────────
    def _paint_panel(self, spin: int) -> None:
        """Record the background-jobs panel: its own message, kept by the kernel as the
        window's bottom-most (re-sent below any newer message); nothing to show →
        deleted."""
        panel = background.panel(self, spin, self.fits_one_message)
        if panel is None:
            self.show_bottom_frame(PANEL_FRAME, None)
        else:
            self.show_bottom_frame(PANEL_FRAME, panel[1], panel[0])

    def _ensure_background_updater(self):
        if self.background_updater_task is None or self.background_updater_task.done():
            self.background_updater_task = asyncio.create_task(self._background_updater())

    async def _background_updater(self):
        """The panel's clock and pruning ticks, while it shows rows — independent of the
        turn, the heartbeat and the CLI process."""
        spin = 0
        try:
            while True:
                now = time.monotonic()
                self._probe_jobs()
                background.prune_finished(self, now)
                self._paint_panel(spin)
                if not background.visible_tasks(self, now):
                    break  # the panel is retired
                await asyncio.sleep(UPDATER_INTERVAL)
                spin += 1
        except asyncio.CancelledError:
            pass
        finally:
            self.background_updater_task = None

    async def _quiesce(self, name: str):
        """Cancel a periodic painter task and wait for it to fully unwind."""
        t = getattr(self, name)
        setattr(self, name, None)
        if t is not None:
            t.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await t

    async def _stop_heartbeat(self):
        await self._quiesce("heartbeat_task")

    # ── Turn boundary + settle ─────────────────────────────────────
    async def _finalize_turn(
        self, result_text, subtype="success", is_error=False, context_window=None
    ):
        """The turn boundary, atomic vs submit()'s ack+queue: close the turn and queue
        its settle (`_settle_turn`) as a send step. No platform call under the lock."""
        async with self.lock:
            # busy stays True; the reader clears it atomically with the kill/keep
            # decision, so a message right after the boundary queues for the next turn
            boundary = self.turn_seq
            self.turn_seq += 1  # the result closes this turn; later acks belong to the next
            # this turn's initiator goes; every queued message whose replay has not come
            # stays queued, ack on screen: the CLI runs it as the next turn
            self.pending_writes = [
                e
                for e in self.pending_writes
                if not (e["is_initiator"] and e.get("turn_seq", 0) <= boundary)
            ]
            # a message ack'd during this turn sits below its card: the output goes below it
            below_queued = any(e.get("turn_seq", 0) <= boundary for e in self.pending_writes)
            turn, ended = self.turn, time.monotonic()
            self.turn = TurnState()
            self.turn_open = False
            self.background_labels = {}  # here, not in the settle: the next turn's go on
            if context_window:
                self.context_window = context_window
            ans = finalize_answer(
                turn,
                result_text,
                subtype=subtype,
                is_error=is_error,
                cancel_requested=self.cancel_requested,
                agent=self.agent_name,
            )
            self._send_in_order(lambda: self._settle_turn(turn, ans, below_queued, ended))
            LOGGER.info(
                "turn end (%s): seq %d→%d, %.0fs, %s tokens, %s, pending=%d",
                self.name,
                boundary,
                self.turn_seq,
                ended - turn.started,
                turn.tok_base + turn.tok_latest,
                subtype,
                len(self.pending_writes),
            )

    async def _settle_turn(self, turn: TurnState, ans, below_queued: bool, ended: float):
        """Render a closed turn: the summary line, then the timeline folded into collapsed
        expandable messages, above the answer messages (suggestion
        buttons on the last), then attachments. Every piece lands, in order.
        `below_queued`: a message ack'd during the turn is still queued, so message 1 is
        sent below its ack instead of landing on the card above it."""
        card_id = turn.card_id
        if below_queued and card_id is not None:
            await self.delete(card_id)
            card_id = None
        lines, _, _ = render_timeline(turn.timeline, drop_last_text=ans.completed)
        pages = expandable_pages(
            self._summary(turn, ended, ans.completed), lines, self.adapter.page_size
        )
        landed = False
        if card_id is not None:
            landed = await self.land_frame(card_id, None, pages)
            if not landed:
                await self.delete(card_id)
        if landed:  # the card is message 1: the rest follow it
            await self.send_md(pages, first_id=card_id)
        else:
            await self.send_md(pages, reply_to=turn.reply_to)
        markup = self._suggestion_markup(ans.options) if ans.options else None
        await self.send_rich(ans.text, reply_markup=markup)
        for p in ans.attachments:
            try:
                await self.send_file(Path(p).expanduser())
            except (RuntimeError, OSError) as e:
                await self.send(f"⚠️ could not attach {p}: {e}")
                self._core.notice(
                    Notice(
                        NOTICE_NOT_DELIVERED, self.window, f"attachment {p}", error=error_text(e)
                    )
                )
        await self._settle_cancel_note()  # settle a '/cancel' bubble at the turn boundary
        self._note_transcript_offset()
        background.prune_finished(self, time.monotonic())
        if background.visible_tasks(self, time.monotonic()):
            self._ensure_background_updater()
        self._save()

    async def _heartbeat(self):
        try:
            while True:
                await asyncio.sleep(EDIT_INTERVAL)
                if not self.turn_open:
                    return  # the turn closed; the next turn's open re-arms
                turn = self.turn
                if turn.card_id is None:
                    continue  # the card is still queued behind earlier output
                quiet = time.monotonic() - self.last_reader_event
                if quiet > STALL_WARN and not self._stall_warned:
                    self._stall_warned = True
                    LOGGER.warning("reader quiet %.0fs on a live turn (%s)", quiet, self.name)
                self.spin += 1
                # a frame: the kernel shows the newest one when a slot is spare
                self.show_frame(turn.card_id, self._live_view(turn))
        except asyncio.CancelledError:
            pass  # normal stop
        except Exception as error:
            # the painter died mid-turn (not a clean cancel) — the card freezes at its
            # last frame, the exact fault behind a "spinner stuck on one number" report
            LOGGER.exception("heartbeat died mid-turn (%s): panel frozen", self.name)
            notice_exception(self._core.notice, self.window, "live card heartbeat", error)

    def _suggestion_markup(self, options):
        token = uuid.uuid4().hex[:8]
        self._suggested[token] = {"options": options}
        while len(self._suggested) > 20:  # bounded — only recent turns' buttons stay live
            self._suggested.pop(next(iter(self._suggested)))
        return ui.suggestion_buttons(options, token)

    @action("sug")
    async def _on_suggestion(self, ctx, arg):
        token, _, idx = arg.partition(":")
        info = self._suggested.pop(token, None)
        if ctx.message is not None:
            await self.set_markup(ctx.message.message_id, None)  # clear the tapped/dead button
        if info and idx.isdigit():
            option = info["options"][int(idx)]
            # record the pick as a visible message so the tap is confirmed even if
            # routing fails; the turn then replies to it (a bot can't post as the user)
            recorded = await self.send(f"☑️ {option}")
            await self._core.route_as_user(self.window, option, reply_to=recorded)

    # ── Mirror (non-driven sessions from the transcript) ───────────
    async def _mirror_loop(self):
        while True:
            await asyncio.sleep(MIRROR_INTERVAL)
            if self.busy or self.owned:
                continue
            try:
                if await self._mirror_once():
                    self._save()
            except Exception as error:
                LOGGER.exception("mirror failed (%s)", self.name)
                notice_exception(self._core.notice, self.window, "transcript mirror", error)

    async def _mirror_once(self) -> bool:
        path = self._transcript_path()
        if path is None:
            return False
        try:
            size = path.stat().st_size
        except OSError:
            return False
        if self.mirror_offset < 0:
            self.mirror_offset = size
            return True
        if size <= self.mirror_offset:
            return False
        with open(path, "rb") as f:
            f.seek(self.mirror_offset)
            chunk = f.read(size - self.mirror_offset)
        if not chunk.endswith(b"\n"):
            last_nl = chunk.rfind(b"\n")
            if last_nl < 0:
                return False
            chunk = chunk[: last_nl + 1]
        self.mirror_offset += len(chunk)
        texts, tools, images, in_flight = self._mirror_parse(
            chunk.decode("utf-8", errors="replace")
        )
        # the loop only reads the transcript; the messages go out as send steps
        for text in texts:
            self._send_in_order(lambda text=text: self.send_rich(text))
        for media_type, b64 in images:
            self._send_in_order(lambda m=media_type, b=b64: self.send_photo(m, b))
        self._mirror_status(tools, in_flight)
        return True

    def _mirror_status(self, tools, in_flight):
        if tools:
            self.mirror_tools += len(tools)
            self.mirror_recent = (self.mirror_recent + tools)[-5:]
        if in_flight:
            now = time.time()
            if self.mirror_holder is None and not self.mirror_holder_queued:
                self.mirror_start = now
                self.mirror_holder_queued = True

                async def send_holder():
                    self.mirror_holder = await self.send("working…")
                    self.mirror_holder_queued = False
                    self._save()

                self._send_in_order(send_holder)
            if self.mirror_holder is None:
                return
            self.mirror_tick += 1
            frame = "✶✸✹✺✹✸"[self.mirror_tick % 6]
            word = WORDS[(sum(self.window.encode()) + self.mirror_tick // 8) % len(WORDS)]
            elapsed = int(now - self.mirror_start)
            n = self.mirror_tools
            plural = "s" if n != 1 else ""
            head = f"{frame} {word}… ({fmt_duration(elapsed)} · {n} tool call{plural})"
            body = "\n".join(self.mirror_recent) or "thinking"
            view = f"{head}\n{body}"
            if len(view) > MAX_MSG:  # tool lines are uncut; the spinner shows the tail
                view = f"{head}\n…{view[-(MAX_MSG - len(head) - 5) :]}"
            self.show_frame(self.mirror_holder, view)
        elif self.mirror_holder is not None:
            holder, self.mirror_holder = self.mirror_holder, None
            self._send_in_order(lambda: self.delete(holder))
            self.mirror_tools = 0
            self.mirror_recent = []
            self.mirror_tick = 0

    # ── Media routing ──────────────────────────────────────────────
    def _attach_dir(self) -> Path:
        return Path(self.workspace) / "tmp" / "attachments"

    async def _buffer_message(self, message, text):
        """Buffer a message; the window's buffered messages go out as one prompt once
        `MESSAGE_DEBOUNCE` passes with no new message and every download has finished
        (an album, or a forward and its comment, arrive as separate messages)."""
        attachments = message.attachments + message.replied_attachments
        download = asyncio.create_task(self._download_attachments(attachments))
        self.message_buffer.append((message, text, download))
        self.message_last_arrival = asyncio.get_running_loop().time()
        if self.message_flush_task is None:
            self.message_flush_task = asyncio.create_task(self._flush_messages())

    async def _flush_messages(self):
        loop = asyncio.get_running_loop()
        while True:
            quiet_left = self.message_last_arrival + MESSAGE_DEBOUNCE - loop.time()
            if quiet_left > 0:
                await asyncio.sleep(quiet_left)
                continue
            downloading = [d for _, _, d in self.message_buffer if not d.done()]
            if not downloading:
                break
            await asyncio.wait(downloading)
        buffered, self.message_buffer = self.message_buffer, []
        self.message_flush_task = None
        try:
            prompt = self._build_prompt(buffered)
        except Exception:  # nothing awaits this task: log before the messages are lost
            LOGGER.exception("message flush (%s): prompt build failed", self.name)
            raise
        if prompt:
            self.notice_prompt_open = False  # a user's prompt: immediate notices may go again
            await self.submit(prompt, reply_to=buffered[0][0].message_id)

    def _context_prefix(self, message) -> str:
        notes: list[str] = []
        if message.forward_source:
            notes.append(f"[Forwarded message from {message.forward_source}]")
        if message.replied_text is not None:
            rtext = message.replied_text
            has_file = bool(message.replied_attachments)
            if message.quote or rtext or has_file:
                label, quoted = (
                    ("Quoting", message.quote)
                    if message.quote
                    else ("Replying to", rtext or "(attachment)")
                )
                tail = " [with attachment]" if has_file and rtext else ""
                who = message.replied_author or "someone"
                notes.append(f"[{label} {who}: {quoted}{tail}]")
        return "\n".join(notes)

    def _build_prompt(self, buffered) -> str:
        """One prompt from buffered (Inbound, text, finished download) entries: each
        message's context note and text in arrival order, then every saved file."""
        parts: list[str] = []
        paths: list[Path] = []
        for message, text, download in buffered:
            ctx = self._context_prefix(message)
            if ctx:
                parts.append(ctx)
            if text:
                parts.append(text)
            paths += download.result()
        if paths:
            listing = "\n".join(str(p) for p in paths)
            parts.append(f"{ATTACHED_HEAD}\n{listing}\n]")
        block = self._take_notice_block()
        if block:
            parts.append(block)
        return "\n\n".join(parts).strip()

    def _take_notice_block(self) -> str:
        """The held notices as a [Bot notice(s)] block (one counted line each), cleared;
        "" when none is held."""
        if not self.pending_notices:
            return ""
        lines = "\n".join(f"- {notice_line(n)}" for n in self.pending_notices.values())
        self.pending_notices = {}
        return f"[Bot notice(s):\n{lines}\n]"

    def on_notice(self, notice: Notice) -> None:
        """A bot notice for this window's agent: held for the next prompt's [Bot
        notice(s)] block (a repeat of the same kind, what and error counts up). An
        exception or a message not delivered is also submitted at once as its own prompt
        (`submit` queues it behind a running turn), unless a notice prompt already went
        since the user's last prompt: then it rides that next prompt, so a failure on a
        reply to a notice prompt never starts another."""
        key = (notice.kind, notice.what, notice.error)
        held = self.pending_notices.get(key)
        if held is None:
            self.pending_notices[key] = dataclasses.replace(notice)
        else:
            held.count += notice.count
        if self._core.shutting_down:  # no prompt while the bot stops: delivered after it
            self._save_notices()
            return
        if notice.kind == NOTICE_FALLBACK or self.notice_prompt_open or self.notice_task:
            return
        self.notice_task = asyncio.get_running_loop().create_task(self._submit_notices())

    def _save_notices(self) -> None:
        """Keep the held notices with the window, for `on_revive` after a restart."""
        if self.pending_notices:
            self.saved["notices"] = [dataclasses.asdict(n) for n in self.pending_notices.values()]

    async def _submit_notices(self) -> None:
        """Submit the held notices as a prompt of their own; notices raised before this
        task first runs are counted into it."""
        self.notice_task = None
        block = self._take_notice_block()
        if not block:
            return
        self.notice_prompt_open = True
        await self.submit(block)

    async def _download_attachments(self, attachments) -> list[Path]:
        out: list[Path] = []
        for attachment in attachments:
            safe = Path(attachment.name).name if attachment.name else ""
            if safe:
                dest = self._attach_dir() / f"{uuid.uuid4().hex[:8]}__{safe}"
            else:
                ext = "jpg" if attachment.kind == "photo" else "bin"
                dest = self._attach_dir() / f"{uuid.uuid4().hex}.{ext}"
            p = await self.download_file(attachment, dest)
            if p:
                out.append(p)
        return out

    # ── Accounts + pickers ─────────────────────────────────────────
    async def _pick_workspace(self):
        ws = self.plugin.workspaces
        if not ws:
            await self.send(f"📁 workspace · {Path(self.workspace).name} (bot home)")
            return
        if len(ws) == 1:  # nothing to choose, but always show which one
            self.workspace = str(ws[0])
            self._save()
            await self.send(f"📁 workspace · {ws[0].name}")
            return
        choice = await self.menu("📁 Workspace", [(w.name, str(w)) for w in ws])
        if choice:
            self.workspace = choice
            self._save()
            await self.send(f"📁 workspace · {Path(choice).name}")

    async def _pick_account(self, allow_new=True):
        if self.busy:
            # the live turn is still writing the old account's session files; carrying
            # them now would drop the turn's tail. Switch once the turn settles.
            await self.send("busy — /cancel first, then switch account")
            return
        if await self._refused_for_jobs("switch account"):
            return
        cd = self.plugin.default_account_dir
        default_label = f"default ({self._account_name(cd)})"
        options = [(default_label + (" ✓" if self.config_dir is None else ""), "__default__")]
        accounts = {**self.plugin.accounts, **self.plugin.scan_accounts()}
        for name, d in accounts.items():
            if Path(d) == cd:
                continue
            options.append((f"{name}{' ✓' if d == self.config_dir else ''}", d))
        if allow_new:
            options.append(("➕ Log in new account", "__new__"))
        choice = await self.menu("👤 Account", options)
        if choice is None:
            return
        old_dir = self._session_config_dir()
        if choice == "__new__":
            res = await self.plugin.login_new_account(self)
            if res:
                _, cfg = res
                self._switch_account(old_dir, Path(cfg), str(cfg))
                await self.send("✅ logged in — this topic now uses it")
            return
        if choice == "__default__":
            self._switch_account(old_dir, cd, None)
            return
        name = next((n for n, d in accounts.items() if d == choice), str(choice))
        self._switch_account(old_dir, Path(str(choice)), str(choice))
        self.plugin.set_account(name, str(choice))

    def _switch_account(self, old_dir: Path, new_dir: Path, config_dir: str | None):
        """Point this window at another account, carrying its session files so history
        survives (else the new dir has no session → a fresh empty one), and dropping an
        idle process so the next message respawns under the new account."""
        if old_dir != new_dir:
            self._carry_transcript(old_dir, new_dir)
        self.config_dir = config_dir
        self._save()
        self._respawn_idle()  # the resume finds the carried session under the new account

    def _respawn_idle(self):
        """After a config change (per-window model/effort/account/workspace or app-wide
        mode), drop an idle process so the next message respawns with it; a live turn
        is left untouched, and so is a process running a job (`_held_jobs`): the change
        applies at its next spawn."""
        if self.backend_alive() and not self.busy and not self._held_jobs():
            self.backend_end()

    async def _switch_model(self):
        cur = self.model or self.plugin.default_model
        opts = [
            (f"{label}{' ✓' if value == cur else ''}", value)
            for label, value in self.plugin.model_pool()
        ]
        opts.append((f"Default{' ✓' if not self.model else ''}", "__default__"))
        choice = await self.menu("🔀 Switch model", opts)
        if not choice:
            return
        self.model = None if choice == "__default__" else choice
        self.context_window = None  # the backend reports the new model's window
        self._save()
        await self.send(self._setting_note("model"))
        await self._apply_settings()

    async def _pick_model_effort(self):
        if self.plugin.model_pool():  # only ask when there is a choice
            model = await self.menu(
                "🧠 Model",
                [(label, value) for label, value in self.plugin.model_pool()]
                + [("Default", "__default__")],
            )
            if model and model != "__default__":
                self.model = model
        effort = await self.menu(
            "⚡ Effort", [(e, e) for e in self.EFFORTS] + [("default", "__default__")]
        )
        if effort and effort != "__default__":
            self.effort = effort
        self._save()

    def _account_label(self, config_dir: str | None) -> str:
        if config_dir is None:
            return f"default ({self._account_name(self.plugin.default_account_dir)})"
        return self._account_name(Path(config_dir))

    async def _interrupt(self):
        if await self.backend_cancel():
            self.cancel_requested = True

    async def _settle_cancel_note(self):
        """Settle the '/cancel' bubble so it never sticks at 'cancelling…'. Bound to
        every turn-end path — the finalize boundary and the reader teardown — since a
        cancel can end via a result or a dead proc."""
        if self.cancel_msg_id is not None:
            mid, self.cancel_msg_id = self.cancel_msg_id, None
            await self.edit(mid, "✖️ cancelled")

    # ── Class commands ─────────────────────────────────────────────
    @command("/stop", "close this window (session stays on disk)", icon="⏹")
    async def stop(self, ctx):
        question = f"Close topic '{self.name}'? The session stays on disk, resumable with /cli."
        warning = self.close_warning()
        if warning:
            question += f" {warning}"
        choice, _ = await self.ask_buttons(question, [("Yes, close", "yes")])
        if choice != "yes":
            await self.send("kept")
            return
        await self.close()

    def close_warning(self) -> str:
        """Closing ends the process, and a process holding jobs takes them down."""
        held = self._held_jobs()
        return f"This also stops the running job ({', '.join(held)})." if held else ""

    @command("/cancel", "interrupt the running turn", icon="✖️")
    async def cancel(self, ctx):
        if self.busy and self.backend_alive():
            if self.cancel_requested:
                await self.send("already cancelling...")  # one interrupt is enough
                return
            await self._interrupt()
            self.cancel_msg_id = await self.send("cancelling...")
            await self.backend_cancelled()
            return
        running = sum(1 for t in self.background_tasks.values() if t["done"] is None)
        if running:
            await self.send(
                f"no running turn to cancel — {running} background job(s) still going; "
                "/kill stops one"
            )
        else:
            await self.send("nothing to cancel — no turn is running")

    @command("/kill", "stop a running background job", icon="🛑")
    async def kill_job(self, ctx):
        running = {b: t for b, t in self.background_tasks.items() if t["done"] is None}
        if not running:
            await self.send("no running background jobs")
            return
        name = (ctx.args or "").strip() if ctx else ""
        if not name:
            now = time.monotonic()
            options = [
                (f"{t['label'][:40]} · {fmt_duration(int(now - t['start']))}", b)
                for b, t in running.items()
            ]
            bid = await self.menu("🛑 Stop which background job?", options)
            if bid is None:
                return
        else:
            matches = background.match_jobs(running, name)
            if len(matches) != 1:
                labels = ", ".join(running[b]["label"] for b in (matches or running))
                if matches:
                    await self.send(f"'{name}' matches several jobs: {labels} — name one")
                else:
                    await self.send(f"no running job matches '{name}' — running: {labels}")
                return
            bid = matches[0]
        await self._stop_job(bid)

    async def _stop_job(self, bid: str):
        """Stop one background job through the backend."""
        task = self.background_tasks.get(bid)
        if task is None or task["done"] is not None:
            await self.send("that job already ended")
            return
        await self.backend_stop_job(bid, task)

    async def _send_turn_note(self, text: str):
        """Send a note from a command; mid-turn, move the live card below it so the
        turn's finalized reply stays the bottom-most of the turn's messages. Returns
        once both are sent."""
        async with self.lock:  # atomic vs the finalize boundary
            last = self._send_in_order(lambda: self.send(text))
            if self.turn_open:
                last = self._reorder_holder()
        await asyncio.wait([last])

    def _fresh_session(self) -> str | None:
        """Reset to a brand-new session (the old one stays resumable) and drop an idle
        CLI process, so the next turn spawns against the new session; returns the old
        id. Callers refuse while a turn runs."""
        old = self.session_id
        self.session_id = self._new_session_id()
        self.mirror_offset = -1
        self.mirror_holder = None
        self.pending_writes = []
        self.turn = TurnState()
        self._respawn_idle()
        return old

    def _session_label(self) -> str:
        """The current session's short id, for a reply."""
        return self.session_id[:8] if self.session_id else "(on the next message)"

    @command("/clear", "start a fresh session (old one stays resumable)", icon="🧹")
    async def clear(self, ctx):
        if self.busy:
            await self.send("busy — /cancel first, then /clear")
            return
        if await self._refused_for_jobs("/clear"):
            return
        old = self._fresh_session()
        self._save()
        was = f" (old {old[:8]} resumable)" if old else ""
        await self.send(f"cleared — fresh session {self._session_label()}{was}")

    @command("/cli", "resume this session on the PC", icon="💻")
    async def cli(self, ctx):
        if not self.session_id:
            await self.send("no session yet — send a message first")
            return
        line = self._cli_line()
        intro = "Continue this session on your computer — paste this into a terminal:"
        await self.send_rich(f"{intro}\n\n{code_block(line)}")  # a tap-to-copy code block

    @command("/rename", "rename this window (auto-titles if no name given)", icon="✏️")
    async def rename_cmd(self, ctx):
        title = ctx.args or await self._generate_title()
        if not title:
            await self.send("couldn't produce a title")
            return
        title = title.strip()[:128]
        self.title = title
        self.saved["base_name"] = title  # the session title becomes the topic name
        self._write_title(title)
        self._save()
        await self.refresh_title(immediate=True)  # an explicit rename: no rename spacing

    @command("/account", "switch this topic's {agent} account", icon="👤")
    async def account(self, ctx):
        await self._pick_account()

    @command("/mode", "set the {agent} permission mode (app-wide)", icon="🔐")
    async def mode(self, ctx):
        cur = self.plugin.permission_mode
        choice = await self.menu(
            f"🔐 Permission mode (now: {cur})",
            [(f"{m} ✓" if m == cur else m, m) for m in self.PERMISSION_MODES],
        )
        if not choice or choice == cur:
            return
        self.plugin.set_permission_mode(choice)
        await self.backend_set_option("mode", choice)
        running = self._running_mode()
        note = f"permission mode → {choice}"
        if running != choice:  # the process is kept for its job (its end would take it down)
            jobs = ", ".join(self._held_jobs())
            note += f" — this window keeps running {running} until its job ends ({jobs})"
        await self.send(note)

    @command("/status", "show this session's state", icon="🗒", inline=True)
    async def status(self, ctx):
        return (
            f"{self.name} · {'busy' if self.busy else 'idle'} · "
            f"{self._account_label(self.config_dir)} · "
            f"model {self.model or self.plugin.default_model or 'default'} · "
            f"mode {self._mode_label()}"
        )

    def _mode_label(self) -> str:
        running, app = self._running_mode(), self.plugin.permission_mode
        return running if running == app else f"{running} (→ {app} after its job ends)"

    @command("/model", "switch the model, or edit the offered list", icon="🧠")
    async def model_cmd(self, ctx):
        await self.plugin.models_cmd(ctx, self)

    @command("/effort", "switch this window's reasoning effort", icon="⚡")
    async def effort_cmd(self, ctx):
        cur = self.effort
        opts = [(f"{e}{' ✓' if e == cur else ''}", e) for e in self.EFFORTS]
        opts.append((f"default{' ✓' if not cur else ''}", "__default__"))
        choice = await self.menu("⚡ Effort", opts)
        if not choice:
            return
        self.effort = None if choice == "__default__" else choice
        await self.send(self._setting_note("effort"))
        await self._apply_settings()

    @command("/workspace", "switch, add, or remove workspace roots", icon="📁")
    async def workspace_cmd(self, ctx):
        if ctx and ctx.args:  # typed add/rm keeps working from the keyboard
            await self.plugin.workspaces_cmd(ctx)
            return
        roots = self.plugin.roots
        opts = [("🔀 Switch workspace", "switch"), ("➕ Add a root", "add")]
        if roots:
            opts.append(("➖ Remove a root", "remove"))
        opts.append(("📋 List roots", "list"))
        choice = await self.menu(f"📁 Workspace · {Path(self.workspace).name}", opts)
        if choice == "switch":
            await self._switch_workspace()
        elif choice == "add":
            path = await self.pick_dir(title="📂 Add a workspace root")
            if path:
                n = self.plugin.add_root(path)
                await self.send(f"added root '{path}' — {n} workspace(s)")
        elif choice == "remove":
            await self._remove_root()
        elif choice == "list":
            await self.send(self.plugin.roots_text())

    async def _switch_workspace(self):
        ws = self.plugin.workspaces
        if not ws:
            await self.send("no workspace roots configured — add one first")
            return
        choice = await self.menu(
            "🔀 Switch workspace",
            [(w.name + (" ✓" if str(w) == self.workspace else ""), str(w)) for w in ws],
        )
        if not choice or choice == self.workspace:
            return
        if self.busy:
            await self.send("busy — /cancel first, then switch")
            return
        if await self._refused_for_jobs("switch"):
            return
        self.workspace = choice
        self._fresh_session()  # a new cwd is a new session
        self._save()
        label = self._session_label()
        await self.send(f"📁 workspace · {Path(choice).name} — fresh session {label}")

    async def _remove_root(self):
        roots = self.plugin.roots
        if not roots:
            return
        choice = await self.menu("➖ Remove a root", [(g, str(i)) for i, g in enumerate(roots)])
        if choice is None:
            return
        removed = self.plugin.remove_root(int(choice))
        if removed is not None:
            await self.send(f"removed root '{removed}'")

    # ── Text handlers ──────────────────────────────────────────────
    @on_message
    async def prompt_msg(self, ctx):
        await self._buffer_message(ctx.message, ctx.text)

    @on_unknown
    async def skill(self, ctx):
        await self.submit(ctx.text, reply_to=getattr(ctx.message, "message_id", None))


class AgentPlugin(Plugin):
    """The neutral agent plugin: the model pool, workspace roots, accounts, `/login`
    and the `!` / `!!` prefixes. A backend subclass sets `id`, `topics`, and the
    account hooks. Its universal handlers are shared by every agent plugin an app
    includes (the kernel registers an inherited handler once); each runs for the
    plugin owning the window it is typed in."""

    DEFAULT_MODELS: list[list[str]] = []
    DEFAULT_PERMISSION_MODE = "auto"
    shared_stores = (ROOTS_STORE,)  # the workspace roots, one list for every agent plugin

    def __init__(self, roots=None, model=None, models=None, brief=None, permission_mode=None):
        self._init_roots = list(roots) if roots else []
        self._init_model = model
        self._init_models = models
        self._init_permission_mode = permission_mode or self.DEFAULT_PERMISSION_MODE
        self.brief = brief  # app-supplied extra guidance; None → try the bot-home file
        self.brief_resolved: str | None = None
        # account data dir → its newest quota, shared by its windows
        self.quota: dict[str, dict] = {}

    # ── Account hooks (implemented by each backend) ────────────────
    @property
    def default_account_dir(self) -> Path:
        """The default account's data dir."""
        raise NotImplementedError

    def account_name(self, config_dir: Path) -> str:
        """An account's short name, from its data dir."""
        raise NotImplementedError

    def account_dir_for(self, name: str) -> Path:
        """The data dir a new account named `name` logs in to."""
        raise NotImplementedError

    def scan_accounts(self) -> dict[str, str]:
        """Every signed-in account on the machine: name → data dir."""
        raise NotImplementedError

    def account_status(self) -> list[dict]:
        """Per-account sign-in state: `name`, `dir`, `label` rows."""
        raise NotImplementedError

    async def _run_login(self, io, name: str, cfg: Path) -> bool:
        """Sign `cfg` in (new or existing); `io` is a topic or ctx. True when signed in."""
        raise NotImplementedError

    def model_pool(self) -> list[list[str]]:
        """The models the pickers offer: the editable list."""
        return self.models

    # ── Settings: read from the store at each use; an edit writes only its key ──
    def _load(self):
        """At startup: move legacy roots, and record the app's model list, which
        `tgforge model` shows and resets to."""
        move_legacy_roots(self.shared[ROOTS_STORE])
        self.saved[APP_MODELS] = [list(m) for m in (self._init_models or self.DEFAULT_MODELS)]

    @property
    def default_model(self) -> str | None:
        return self.saved.get("model", self._init_model)

    def set_default_model(self, model: str) -> None:
        self.saved["model"] = model

    @property
    def permission_mode(self) -> str:
        """The mode every spawn applies."""
        return self.saved.get("permission_mode", self._init_permission_mode)

    def set_permission_mode(self, mode: str) -> None:
        self.saved["permission_mode"] = mode

    @property
    def accounts(self) -> dict[str, str]:
        return self.saved.get("accounts", {})

    def set_account(self, name: str, config_dir: str) -> None:
        self.saved["accounts"] = {**self.accounts, name: config_dir}

    @property
    def models(self) -> list[list[str]]:
        """The editable model list: the edited one, else the app's."""
        return self.saved.get("models") or self.app_models()

    def app_models(self) -> list[list[str]]:
        """The app's own model list (`models=[…]`, recorded at startup for the CLI)."""
        app = self.saved.get(APP_MODELS)
        if app is not None:
            return app
        return [list(m) for m in (self._init_models or self.DEFAULT_MODELS)]

    @property
    def roots(self) -> list[str]:
        """The workspace roots, one list for every agent plugin (the shared store)."""
        return self.shared[ROOTS_STORE].get("roots") or list(self._init_roots)

    @property
    def workspaces(self) -> list[Path]:
        return resolve_roots(self.roots)

    def _plugin_for(self, ctx) -> AgentPlugin:
        """The agent plugin a shared handler runs for: the one owning the window."""
        topic = getattr(ctx, "topic", None)
        return topic.plugin if isinstance(topic, AgentTopic) else self

    def _agent_plugins(self) -> list[AgentPlugin]:
        """Every agent plugin the app includes, in include order."""
        return [p for p in self.peers if isinstance(p, AgentPlugin)]

    # ── Accounts ───────────────────────────────────────────────────
    def seed_accounts(self):
        known = self.accounts
        for name, d in self.scan_accounts().items():
            if name not in known:
                self.set_account(name, d)

    async def login_new_account(self, topic):
        """Prompt for a name, then run the login flow into a fresh account dir."""
        name = await topic.ask_text(
            "Send a short name for the new account (e.g. work, personal):", timeout=120
        )
        if not name:
            return None
        name = re.sub(r"[^\w.-]", "", name.strip())[:32]
        if not name:
            await topic.send("invalid name — cancelled")
            return None
        cfg = self.account_dir_for(name)
        if not await self._run_login(topic, name, cfg):
            return None
        self.set_account(name, str(cfg))
        return name, str(cfg)

    async def _relogin(self, ctx, name, cfg):
        if await self._run_login(ctx, name, cfg):
            if cfg != self.default_account_dir:
                self.set_account(name, str(cfg))
            await ctx.send(f"✅ '{name}' re-logged in")

    @universal("/login", "credential status; re-login any account", icon="🔑")
    async def login_cmd(self, ctx):
        topic = getattr(ctx, "topic", None)
        agents = [self._plugin_for(ctx)] if isinstance(topic, AgentTopic) else None
        agents = agents or self._agent_plugins() or [self]
        many = len(agents) > 1
        rows: list[tuple[AgentPlugin, dict]] = []
        lines = []
        for plugin in agents:
            plugin_rows = plugin.account_status()
            rows += [(plugin, r) for r in plugin_rows]
            if many:
                lines.append(f"{plugin.id}:")
            lines += [r["label"] for r in plugin_rows]
        await ctx.send("account login status:\n" + "\n".join(lines))
        args = getattr(ctx, "args", "").strip()
        if args:
            row = next(((p, r) for p, r in rows if r["name"] == args), None)
            if not row:
                known = ", ".join(r["name"] for _, r in rows)
                await ctx.send(f"no account '{args}' — known: {known}")
                return
            plugin, r = row
            await plugin._relogin(ctx, r["name"], Path(r["dir"]))
            return

        def label(plugin, r):
            return f"{plugin.id} · {r['name']}" if many else r["name"]

        options = [(label(p, r), f"{i}") for i, (p, r) in enumerate(rows)]
        options += [
            (f"➕ Log in new account{f' ({p.id})' if many else ''}", f"__new__{p.id}")
            for p in agents
        ]
        choice = await ctx.menu("🔑 Re-login an account?", options)
        if not choice:
            return
        if choice.startswith("__new__"):
            plugin = next(p for p in agents if choice == f"__new__{p.id}")
            await plugin.login_new_account(ctx)
            return
        plugin, r = rows[int(choice)]
        await plugin._relogin(ctx, r["name"], Path(r["dir"]))

    # ── Model-list edits (shared by the typed path + the /model menu) ─
    def add_model(self, label: str, value: str) -> None:
        self.saved["models"] = [*self.models, [label, value]]

    def remove_model(self, idx: int) -> list | None:
        models = self.models
        if 0 <= idx < len(models):
            removed = models.pop(idx)
            self.saved["models"] = models
            return removed
        return None

    def reset_models(self) -> None:
        """Back to the app's `models=[...]` list (the library default if none)."""
        self.saved["models"] = self.app_models()

    def models_text(self) -> str:
        launch = self.topics[0]._tg_launch[0]
        lines = [f"models offered at {launch}:"]
        default = self.default_model
        lines += [
            f"  {i}. {label} — {value}{' (default)' if value == default else ''}"
            for i, (label, value) in enumerate(self.models, 1)
        ]
        return "\n".join(lines)

    # ── Workspace-root edits (shared by the typed path + the menu) ─────
    def add_root(self, glob_str: str) -> int:
        self.shared[ROOTS_STORE]["roots"] = [*self.roots, glob_str]
        return len(self.workspaces)

    def remove_root(self, idx: int) -> str | None:
        roots = self.roots
        if 0 <= idx < len(roots):
            removed = roots.pop(idx)
            self.shared[ROOTS_STORE]["roots"] = roots
            return removed
        return None

    def roots_text(self) -> str:
        lines = ["workspace roots (globs):"]
        lines += [f"  {i}. {g}" for i, g in enumerate(self.roots, 1)] or ["  (none)"]
        lines += ["", "resolved:"]
        lines += [f"  • {w}" for w in self.workspaces]
        return "\n".join(lines)

    # ── /model + /workspace list editing (run from an agent window) ─
    async def models_cmd(self, ctx, topic):
        if ctx and ctx.args:  # typed add/rm/reset keeps working from the keyboard
            await self._models_typed(ctx)
            return
        opts = [("🔀 Switch model", "switch"), ("➕ Add a model", "add")]
        if self.models:
            opts.append(("➖ Remove a model", "remove"))
        opts += [("↩️ Reset to defaults", "reset"), ("📋 List models", "list")]
        choice = await topic.menu("🧠 Model", opts)
        if choice == "switch":
            await topic._switch_model()
        elif choice == "add":
            raw = await topic.ask_text("Send the model as `label | model-id` (or just the id):")
            if raw and raw.strip():
                label, _, value = raw.strip().partition("|")
                label, value = label.strip(), (value.strip() or label.strip())
                self.add_model(label, value)
                await topic.send(f"added '{label}' → {value}")
        elif choice == "remove":
            pick = await topic.menu(
                "➖ Remove a model",
                [(f"{label} — {value}", str(i)) for i, (label, value) in enumerate(self.models)],
            )
            if pick is not None:
                removed = self.remove_model(int(pick))
                if removed:
                    await topic.send(f"removed '{removed[0]}'")
        elif choice == "reset":
            self.reset_models()
            await topic.send("model list reset to defaults")
        elif choice == "list":
            await topic.send(self.models_text())

    async def _models_typed(self, ctx):
        await ctx.send(self.edit_models(ctx.args))

    def edit_models(self, args: str) -> str:
        """`add <label>|<value>`, `rm <n or label or value>`, `reset`, else the list:
        the typed `/model` forms and `tgforge model`; returns the reply."""
        parts = args.split(maxsplit=1)
        sub = parts[0].lower() if parts else ""
        rest = parts[1].strip() if len(parts) > 1 else ""
        if sub == "add" and rest:
            label, _, value = rest.partition("|")
            label, value = label.strip(), (value.strip() or label.strip())
            self.add_model(label, value)
            return f"added '{label}' → {value}"
        if sub in ("rm", "remove", "del") and rest:
            idx = None
            if rest.isdigit():
                idx = int(rest) - 1
            else:
                idx = next((j for j, (la, va) in enumerate(self.models) if rest in (la, va)), None)
            removed = self.remove_model(idx) if idx is not None else None
            return f"removed '{removed[0]}'" if removed else f"no model matching '{rest}'"
        if sub == "reset":
            self.reset_models()
            return "model list reset to defaults"
        return self.models_text()

    async def workspaces_cmd(self, ctx):
        parts = ctx.args.split(maxsplit=1)
        sub = parts[0].lower() if parts else ""
        rest = parts[1].strip() if len(parts) > 1 else ""
        if sub == "add" and rest:
            n = self.add_root(rest)
            await ctx.send(f"added root '{rest}' — {n} workspace(s)")
        elif sub in ("rm", "remove", "del") and rest:
            if rest.isdigit():
                idx = int(rest) - 1
            else:
                idx = self.roots.index(rest) if rest in self.roots else None
            removed = self.remove_root(idx) if idx is not None else None
            await ctx.send(f"removed root '{removed}'" if removed else f"no root matching '{rest}'")
        else:
            await ctx.send(self.roots_text())

    # ── One-shot shell prefixes (! / !!) ───────────────────────────
    @prefix("!!", "run a shell command and hand the result to the agent")
    async def bang_bang(self, ctx):
        return await self._one_shot(ctx, chain=True)

    @prefix("!", "run a one-shot shell command in the agent's workspace")
    async def bang(self, ctx):
        return await self._one_shot(ctx, chain=False)

    async def _one_shot(self, ctx, chain):
        if not isinstance(ctx.topic, AgentTopic):
            return False  # only inside an agent window; else fall through the chain
        topic = ctx.topic
        raw = ctx.text[2:] if chain else ctx.text[1:]
        cmd = raw.strip()
        if not cmd:
            return
        env = {
            **os.environ,
            "PATH": f"{Path.home()}/.local/bin:" + os.environ.get("PATH", ""),
        }
        hid = await topic.send(f"$ {cmd}\n…")
        buf: list[str] = []
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        view = {"last": 0.0, "timer": None}  # the pump's last frame time, its pending timer

        def show_view():
            view["timer"] = None
            view["last"] = time.monotonic()
            topic.show_frame(
                hid, shell_view(cmd, buf, "…"), shell_md(cmd, buf, "", topic.fits_one_message)
            )

        status = ""
        proc = None
        try:
            proc = await topic.spawn(  # reaped with the window if it closes mid-run
                "/bin/sh",
                "-c",
                cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=topic.workspace,
                env=env,
                start_new_session=True,
            )

            async def pump():
                # only reads and records: a frame at most every EDIT_INTERVAL, never
                # waited on; output held back by that interval is shown when it ends (a
                # timer), not only when the next chunk comes
                kept = 0
                while True:
                    data = await proc.stdout.read(SHELL_READ_SIZE)
                    if not data:
                        break
                    buf.append(decoder.decode(data))
                    kept += len(buf[-1])
                    while len(buf) > 1 and kept - len(buf[0]) >= SHELL_KEEP_CHARS:
                        kept -= len(buf.pop(0))
                    if hid is None:
                        continue
                    wait = view["last"] + EDIT_INTERVAL - time.monotonic()
                    if wait <= 0:
                        show_view()
                    elif view["timer"] is None:
                        view["timer"] = asyncio.get_running_loop().call_later(wait, show_view)

            await asyncio.wait_for(pump(), timeout=SHELL_TIMEOUT)
            await proc.wait()
            status = f"exit {proc.returncode}"
        except TimeoutError:
            if proc is not None:
                await reap(proc)  # the command's whole process group
            status = (
                f"timed out after {SHELL_TIMEOUT}s — stopped: the command and every process "
                "it started were killed; for a longer run, detach it (nohup … &) and watch its log"
            )
        except Exception as exc:
            status = f"failed to run: {exc}"
        finally:
            if view["timer"] is not None:
                view["timer"].cancel()  # the final view below replaces it
            if proc is not None:
                await reap(proc)  # never leave the shell child running on any exit path
        tail = decoder.decode(b"", final=True)  # a cut-off multi-byte character at the end
        if tail:
            buf.append(tail)
        result = shell_view(cmd, buf, status)
        if hid is not None:
            await topic.land_frame(hid, result, shell_md(cmd, buf, status, topic.fits_one_message))
        payload = f"[shell result]\n{result}"
        if chain:
            await topic.submit(payload)
        elif hid is not None:
            await ctx.offer_handoff(
                hid, payload, service="agent.prompt", label="↪ hand to the agent"
            )

    # ── Cross-plugin service ───────────────────────────────────────
    @service("agent.prompt")
    async def agent_prompt(self, ctx, text):
        if isinstance(ctx.topic, AgentTopic):
            await ctx.topic.submit(text)
        else:
            launches = ", ".join(p.topics[0]._tg_launch[0] for p in self._agent_plugins())
            await ctx.send(f"open a {launches} window to hand this to the agent")
