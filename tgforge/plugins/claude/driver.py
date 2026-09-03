"""ClaudeTopic + Claude plugin: the Claude CLI agent as a window class.

Each `/claude` window is one `ClaudeTopic` instance that spawns and drives the CLI
(stream-json in/out), renders the live status holder and finalized turn, mirrors
non-driven sessions from the transcript on disk, and tracks background tasks. The
per-window session state persists in the window store; account/model/workspace
config is the plugin's, in the plugin store. Turn-shaping is delegated to the pure
render/events/session/background modules.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import re
import shutil
import time
import uuid
from pathlib import Path

from tgforge.base import ui
from tgforge.base.kernel import (
    Plugin,
    Topic,
    action,
    command,
    launch,
    on_message,
    on_unknown,
    prefix,
    reap,
    service,
    universal,
)
from tgforge.base.service import service_manager
from tgforge.base.ui import (
    MAX_MSG,
    chunks,
    expandable,
    fmt_duration,
    mdv2_escape,
    shell_md,
    shell_view,
    to_md,
)
from tgforge.plugins.claude import background, setup
from tgforge.plugins.claude.config import (
    DEFAULT_MODELS,
    claude_bin,
    claude_dir,
    resolve_roots,
)
from tgforge.plugins.claude.events import (
    has_background_launch,
    is_prompt_replay,
    mirror_parse,
    prompt_block_count,
)
from tgforge.plugins.claude.render import (
    WORDS,
    fmt_tokens,
    render_timeline,
    status_head,
)
from tgforge.plugins.claude.session import TurnState, apply_event, finalize_answer

LOGGER = logging.getLogger("tgforge")

MAX_BLOCK_BODY = 1500
EDIT_INTERVAL = 2.5
MAX_OUTPUT_MSGS = 8
ALBUM_DEBOUNCE = 2.0
RELEASE_IDLE_SEC = 1800
UPDATER_INTERVAL = 5.0
MIRROR_INTERVAL = 3.0
SHELL_TIMEOUT = 120
QUOTA_THRESHOLDS = (0.25, 0.50, 0.75, 0.90, 0.95)

# Claude CLI --permission-mode values, passed on every spawn. The app default is
# `auto` (a bot has no human to answer prompts); set it on the plugin or via /mode.
PERMISSION_MODES = ("auto", "acceptEdits", "bypassPermissions", "dontAsk", "manual", "plan")
DEFAULT_PERMISSION_MODE = "auto"


SESSION_BRIEF = (
    "Interactive tools (AskUserQuestion) are unavailable — "
    "when a decision is needed, state it plainly and stop. Always end your message "
    "with one line `[[suggest]] reply one | reply two | reply three` predicting the "
    "user's three most probable next replies — the bot turns each into a button "
    "that sends that text as the user's next message. Files the user attaches or "
    "forwards (any type, photos included) are downloaded locally and their paths "
    "listed in the prompt's [Attached file(s)] block; if the user mentions a sent "
    "file with no path, look for the newest matching file there (20MB bot-API "
    "download cap). To send the user a file, end your message with one line per "
    "file: `[[attach]] /absolute/path` — sent after the text as a photo (images "
    "≤10MB) or a document (≤50MB); the marker line never renders. Do "
    "not use Markdown tables in replies — Telegram renders them as raw text, not a "
    "grid; present tabular data as short labeled lines or a bullet list instead."
)


def _project_dir(config_dir: Path, workspace: Path) -> Path:
    return config_dir / "projects" / ("-" + str(workspace).replace("/", "-").lstrip("-"))


@launch("/claude", "open a Claude agent window")
class ClaudeTopic(Topic):
    id = "claude"
    icon = "🤖"
    menu_label = "Claude"

    def __init__(self, core, thread_id, name, saved):
        super().__init__(core, thread_id, name, saved)
        # persisted (window store)
        self.session_id = str(uuid.uuid4())
        self.model: str | None = None
        self.effort: str | None = None
        self.config_dir: str | None = None
        self.workspace = ""
        self.title: str | None = None  # the session name shown as the title suffix
        self.mirror_offset = -1
        # runtime
        self.owned = False
        self.busy = False
        self.proc: asyncio.subprocess.Process | None = None
        self.lock = asyncio.Lock()
        self.reader_task: asyncio.Task | None = None
        self.heartbeat_task: asyncio.Task | None = None
        self.mirror_task: asyncio.Task | None = None
        self.holder_id: int | None = None
        self.cur_reply_to: int | None = None
        self.turn_start = 0.0
        self.word_seed = 0
        self.spin = 0
        self.pending_writes: list[dict] = []
        self.cancel_requested = False
        self.turn = TurnState()
        self.release_task: asyncio.Task | None = None
        self.background_tasks: dict[str, dict] = {}
        self.background_labels: dict[str, str] = {}
        self.background_probe_at = 0.0
        self.background_updater_task: asyncio.Task | None = None
        self.last_final_id: int | None = None
        self.last_final_body: tuple | None = None
        self.last_final_markup: list | None = None
        self.mirror_holder: int | None = None
        self.mirror_tools = 0
        self.mirror_recent: list[str] = []
        self.mirror_start = 0.0
        self.mirror_tick = 0
        self._suggested: dict[str, dict] = {}
        self._albums: dict[str, dict] = {}
        self._quota_alerts: dict[tuple, dict] = {}

    # ── Convenience ────────────────────────────────────────────────
    @property
    def bot(self):
        return self._core.bot

    @property
    def chat_id(self):
        return self._core.chat_id

    def _env_tag(self) -> str:
        """The `[via Telegram bot · …]` locator prepended to a turn's brief: which
        bot, its process manager, and where it runs. Absent fields are dropped."""
        cfg = self._core.config
        name = (cfg.service or cfg.bot_username or "tgforge").strip()
        parts = [f"via Telegram bot · {name[:1].upper() + name[1:]}", service_manager()]
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

    def _default_workspace(self) -> str:
        ws = self.plugin.workspaces
        return str(ws[0]) if ws else str(self._core.config.home_path)

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
        self._start_mirror()

    async def on_revive(self):
        data = self.saved.get("session", {})
        self.session_id = data.get("session_id", self.session_id)
        self.model = data.get("model")
        self.effort = data.get("effort")
        self.config_dir = data.get("config_dir")
        self.workspace = data.get("workspace") or self._default_workspace()
        self.title = data.get("title")
        self.mirror_offset = data.get("mirror_offset", -1)
        self.mirror_holder = data.get("mirror_holder")
        self.mirror_tools = data.get("mirror_tools", 0)
        self.mirror_start = data.get("mirror_start", 0.0)
        self._start_mirror()

    async def on_close(self):
        for t in (
            self.reader_task,
            self.heartbeat_task,
            self.mirror_task,
            self.background_updater_task,
            self.release_task,
        ):
            if t is not None:
                t.cancel()
        if self.proc:
            await reap(self.proc)  # close the transport so the CLI child never leaks

    def _save(self):
        self.saved["session"] = {
            "session_id": self.session_id,
            "workspace": self.workspace,
            "mirror_offset": self.mirror_offset,
            **({"title": self.title} if self.title else {}),
            **({"model": self.model} if self.model else {}),
            **({"effort": self.effort} if self.effort else {}),
            **({"config_dir": self.config_dir} if self.config_dir else {}),
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

    # ── Session env ────────────────────────────────────────────────
    def _login_env(self, config_dir: Path | None = None) -> dict:
        env = {
            **os.environ,
            "PATH": f"{Path.home()}/.local/bin:{Path.home()}/.cargo/bin:"
            + os.environ.get("PATH", ""),
        }
        if config_dir is not None:
            env["CLAUDE_CONFIG_DIR"] = str(config_dir)
        return env

    def _session_config_dir(self) -> Path:
        return Path(self.config_dir) if self.config_dir else self.plugin.claude_dir

    def _proj_dir(self) -> Path:
        return _project_dir(self._session_config_dir(), Path(self.workspace))

    def _jsonl(self) -> Path:
        return self._proj_dir() / f"{self.session_id}.jsonl"

    # ── Turn driving ───────────────────────────────────────────────
    async def _ensure_proc(self):
        if self.proc is not None and self.proc.returncode is None:
            return
        if self.reader_task is not None and not self.reader_task.done():
            self.reader_task.cancel()
        if self.heartbeat_task is not None:
            self.heartbeat_task.cancel()
        is_new = not self._jsonl().exists()
        cmd = [
            self.plugin.claude_bin,
            "-p",
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
            "--verbose",
            "--include-partial-messages",
            "--replay-user-messages",
        ]
        model = self.model or self.plugin.default_model
        if model:
            cmd += ["--model", model]
        if self.effort:
            cmd += ["--effort", self.effort]
        cmd += ["--session-id", self.session_id] if is_new else ["--resume", self.session_id]
        cmd += ["--permission-mode", self.plugin.permission_mode]
        env = self._login_env(self._session_config_dir())
        env["TELEGRAM_THREAD_ID"] = str(self.thread_id or 0)
        env["TELEGRAM_BOT_DRIVEN"] = "1"
        proc = await self.spawn(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            cwd=self.workspace,
            env=env,
            limit=16 * 1024 * 1024,
        )
        self.proc = proc
        self.owned = True
        self.reader_task = asyncio.create_task(self._reader())
        self.heartbeat_task = asyncio.create_task(self._heartbeat())

    async def _write_prompt(self, prompt: str):
        # A queued write can race the reader's teardown (which closes stdin + kills the
        # proc outside the lock); drop it safely rather than throwing out of the handler.
        if self.proc is None or self.proc.returncode is not None or self.proc.stdin.is_closing():
            return
        obj = {
            "type": "user",
            "message": {"role": "user", "content": [{"type": "text", "text": prompt}]},
        }
        try:
            self.proc.stdin.write((json.dumps(obj) + "\n").encode())
            await self.proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    async def _open_holder(self, reply_to=None, reuse_id=None):
        if self.background_updater_task is not None and not self.background_updater_task.done():
            self.background_updater_task.cancel()
        if self.last_final_id is not None and self.last_final_body is not None:
            md, plain = self.last_final_body
            await self.edit_md(self.last_final_id, md, plain, reply_markup=self.last_final_markup)
            self.last_final_id = None
            self.last_final_markup = None
        self.turn = TurnState()
        self.turn_start = time.monotonic()
        self.word_seed = random.randrange(len(WORDS))
        self.busy = True
        self.cur_reply_to = reply_to
        self.spin = 0
        head = status_head(self.word_seed, 0, 0, 0)
        if reuse_id is not None:
            await self.edit(reuse_id, head)
            self.holder_id = reuse_id
        else:
            self.holder_id = await self.send(head, reply_to=reply_to)

    async def _reorder_holder(self):
        old = self.holder_id
        elapsed = int(time.monotonic() - self.turn_start)
        self.spin += 1
        tokens = self.turn.tok_base + self.turn.tok_latest
        head = status_head(self.word_seed, self.spin, elapsed, tokens)
        self.holder_id = await self.send(head, reply_to=self.cur_reply_to)
        if old is not None:
            await self.delete(old)

    async def submit(self, prompt, reply_to=None):
        """The single prompt entry point (on_message, a skill slash, the ! chain,
        and the agent.prompt service all funnel here)."""
        async with self.lock:
            if self.busy and (self.proc is None or self.proc.returncode is not None):
                self.busy = False
                self.holder_id = None
                self.pending_writes = []
            try:
                await self._ensure_proc()
            except Exception as exc:
                await self.send(f"failed to launch: {exc}")
                return
            if not prompt.startswith("/"):
                brief = f"{self._env_tag()} {SESSION_BRIEF}"
                extra = self._app_brief()
                prompt = f"{prompt}\n\n{brief}" + (f"\n{extra}" if extra else "")
            if self.busy:
                ack = await self.send("queued ✓", reply_to=reply_to)
                self.pending_writes.append(
                    {"is_initiator": False, "bubble_id": ack, "reply_to": reply_to}
                )
                await self._write_prompt(prompt)
            else:
                self.busy = True
                self.cancel_requested = False
                await self._open_holder(reply_to=reply_to)
                self.pending_writes.append(
                    {"is_initiator": True, "bubble_id": None, "reply_to": reply_to}
                )
                await self._write_prompt(prompt)

    async def on_restarted(self):
        """This topic asked for the restart: re-enter the session so it resumes on its
        own — whoever hit restart (a running turn or the user) is handed back in."""
        await self.submit("🔄 Bot restarted and is back online — continue where you left off.")

    async def on_shutdown(self):
        """A graceful restart: settle a live turn holder into a final bubble (drop the
        spinner, keep the timeline) so nothing is left mid-render; the session revives
        and re-mirrors after the restart."""
        if self.holder_id is None:
            return
        note = "⏸ interrupted by a restart — resuming shortly"
        events = self._events_block(drop_last_text=False)
        if events:
            await self.edit_md(self.holder_id, f"{events[0]}\n\n{note}", f"{events[1]}\n\n{note}")
        else:
            await self.edit(self.holder_id, note)
        self.holder_id = None

    async def _warn_interrupted(self):
        """Flag a turn cut short by a dead process — but stay silent on a graceful
        bot restart, which re-adopts the session (nothing was lost, no alarm needed)."""
        if self.holder_id is not None and not self._core.shutting_down:
            await self.edit(
                self.holder_id,
                "⚠️ the session process exited mid-turn — nothing on disk was lost. "
                "Send your message again to retry, or /cli to resume on the PC.",
            )

    async def _reader(self):
        has_background_tasks = False
        try:
            async for raw in self.proc.stdout:
                line = raw.decode(errors="replace").strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if self.release_task is not None:
                    self.release_task.cancel()
                    self.release_task = None
                t = ev.get("type")
                if not self.busy and not self.pending_writes and t in ("assistant", "stream_event"):
                    await self._open_holder()
                if t == "rate_limit_event":
                    await self._quota_alert(ev)
                if t == "assistant":
                    self.background_labels.update(background.launch_labels(ev))
                if t == "user":
                    background.register_task(self, ev)
                    background.mark_done(self, ev)
                apply_event(self.turn, ev)
                if t == "user" and is_prompt_replay(ev) and self.pending_writes:
                    folded = False
                    n = min(prompt_block_count(ev), len(self.pending_writes))
                    for _ in range(n):
                        entry = self.pending_writes.pop(0)
                        if self.holder_id is None:
                            await self._open_holder(
                                reply_to=entry["reply_to"], reuse_id=entry["bubble_id"]
                            )
                        elif not entry["is_initiator"]:
                            if entry["bubble_id"] is not None:
                                await self.delete(entry["bubble_id"])
                            folded = True
                    if folded:
                        await self._reorder_holder()
                elif t == "result":
                    if self.holder_id is None:
                        await self._open_holder(reply_to=self.cur_reply_to)
                    await self._finalize_turn(
                        ev.get("result", ""),
                        subtype=ev.get("subtype", "success"),
                        is_error=bool(ev.get("is_error")),
                    )
                    if self.pending_writes:
                        continue
                    self.busy = False
                    running = any(x["done"] is None for x in self.background_tasks.values())
                    if has_background_tasks or running:
                        has_background_tasks = False
                        if self.release_task is not None:
                            self.release_task.cancel()
                        self.release_task = asyncio.create_task(self._release_idle())
                        continue
                    try:
                        self.proc.stdin.close()
                    except (BrokenPipeError, ConnectionResetError, OSError):
                        pass
                    self.proc.kill()
                    break
                if has_background_launch(ev):
                    has_background_tasks = True
        except Exception:
            LOGGER.exception("reader error (%s)", self.name)
        finally:
            if self.release_task is not None:
                self.release_task.cancel()
                self.release_task = None
            await self._warn_interrupted()
            if self._jsonl().exists():
                self.mirror_offset = self._jsonl().stat().st_size
            self.proc = None
            self.busy = False
            self.holder_id = None
            self.pending_writes = []
            self.cur_reply_to = None
            self.owned = False
            if self.heartbeat_task is not None:
                self.heartbeat_task.cancel()
                self.heartbeat_task = None
            if self.background_updater_task is not None:
                self.background_updater_task.cancel()
                self.background_updater_task = None
            self.reader_task = None
            self._save()

    async def _release_idle(self):
        try:
            await asyncio.sleep(RELEASE_IDLE_SEC)
        except asyncio.CancelledError:
            return
        self.release_task = None
        if self.proc is not None and self.proc.returncode is None:
            try:
                self.proc.stdin.close()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            self.proc.kill()

    def _ensure_background_updater(self):
        if self.background_updater_task is not None and not self.background_updater_task.done():
            self.background_updater_task.cancel()
        self.background_updater_task = asyncio.create_task(self._background_updater())

    async def _background_updater(self):
        spin = 0
        try:
            while self.background_tasks and self.last_final_id is not None:
                await asyncio.sleep(UPDATER_INTERVAL)
                spin += 1
                background.mark_orphans(self)
                panel = background.panel(self, spin)
                if panel is None or self.last_final_body is None:
                    break
                md, plain = self.last_final_body
                await self.edit_md(
                    self.last_final_id,
                    f"{md}\n\n{panel[0]}",
                    f"{plain}\n\n{panel[1]}",
                    reply_markup=self.last_final_markup,
                )
        except asyncio.CancelledError:
            pass
        finally:
            self.background_updater_task = None

    def _events_block(self, drop_last_text=True):
        tokens = self.turn.tok_base + self.turn.tok_latest
        lines, n_tools, n_narr = render_timeline(self.turn.timeline, drop_last_text)
        if not lines and tokens == 0:
            return None
        dur = fmt_duration(int(time.monotonic() - self.turn_start))
        parts = []
        if n_tools:
            parts.append(f"🔧 {n_tools}")
        if n_narr:
            parts.append(f"💬 {n_narr}")
        parts += [dur, f"{fmt_tokens(tokens)} tokens"]
        header = " · ".join(parts)
        body = "\n\n".join(lines)
        if len(body) > MAX_BLOCK_BODY:
            body = "…" + body[-MAX_BLOCK_BODY:]
        return expandable(header, body), (header + ("\n\n" + body if body else ""))

    async def _quota_alert(self, ev):
        try:
            info = ev.get("rate_limit_info") or {}
            kind = info.get("rateLimitType")
            util = info.get("utilization")
            resets = info.get("resetsAt")
            if kind not in ("five_hour", "seven_day"):
                return
            if not isinstance(util, (int, float)):
                return
            key = (self.config_dir or "default", kind)
            st = self._quota_alerts.get(key)
            if st is None or st.get("resets") != resets:
                st = {"resets": resets, "max": 0.0}
                self._quota_alerts[key] = st
            crossed = [x for x in QUOTA_THRESHOLDS if util >= x > st["max"]]
            if not crossed:
                return
            st["max"] = max(crossed)
            label = "5-hour" if kind == "five_hour" else "weekly"
            when = ""
            if isinstance(resets, (int, float)):
                mins = max(0, int((resets - time.time()) / 60))
                when = (
                    f" · resets in ~{mins // 60}h{mins % 60:02d}m"
                    if mins >= 60
                    else f" · resets in ~{mins}m"
                )
            over = " (overage)" if info.get("isUsingOverage") else ""
            await self.send(
                f"⚠️ {label} quota {round(util * 100)}% used"
                f" — crossed {round(max(crossed) * 100)}%{over}{when}"
            )
        except Exception as exc:
            LOGGER.warning("quota alert failed: %r", exc)

    async def _finalize_turn(self, result_text, subtype="success", is_error=False):
        holder_id = self.holder_id
        self.busy = False
        self.holder_id = None
        ans = finalize_answer(
            self.turn,
            result_text,
            subtype=subtype,
            is_error=is_error,
            cancel_requested=self.cancel_requested,
        )
        events = self._events_block(drop_last_text=ans.completed)
        md_answer = to_md(ans.text)
        combined_md = f"{events[0]}\n\n{md_answer}" if events else md_answer
        combined_plain = f"{events[1]}\n\n{ans.text}" if events else ans.text
        panel = background.panel(self, self.spin)
        if panel:
            prefix_md, prefix_plain = combined_md, combined_plain
            combined_md = f"{combined_md}\n\n{panel[0]}"
            combined_plain = f"{combined_plain}\n\n{panel[1]}"
            if any(x["done"] is None for x in self.background_tasks.values()):
                self.last_final_id = holder_id
                self.last_final_body = (prefix_md, prefix_plain)
                self.last_final_markup = None
            else:
                self.background_tasks = {}
                self.background_labels = {}
                self.last_final_id = None
                self.last_final_markup = None
        last_id = holder_id
        last_body = None  # a chunked turn moves the panel to the last chunk; track its body
        if len(combined_md) <= MAX_MSG:
            if not await self.edit_md(holder_id, combined_md, combined_plain):
                sent = await self.send_rich(ans.text)
                last_id = sent or last_id
        elif events:
            await self.edit_md(holder_id, events[0], events[1])
            for i, chunk in enumerate(chunks(ans.text)):
                if i >= MAX_OUTPUT_MSGS:
                    await self.send("(output truncated)")
                    break
                sent = await self.send_rich(chunk)
                last_id = sent or last_id
                last_body = (to_md(chunk), chunk)
        else:
            for i, chunk in enumerate(chunks(ans.text)):
                if i >= MAX_OUTPUT_MSGS:
                    await self.send("(output truncated)")
                    break
                if i == 0:
                    await self.edit_rich(holder_id, chunk)
                    last_body = (to_md(chunk), chunk)
                else:
                    sent = await self.send_rich(chunk)
                    last_id = sent or last_id
                    last_body = (to_md(chunk), chunk)
        for p in ans.attachments:
            try:
                await self.send_file(Path(p).expanduser())
            except (RuntimeError, OSError) as e:
                await self.send(f"⚠️ could not attach {p}: {e}")
        if ans.options and last_id is not None:
            kb = await self._attach_suggestions(last_id, ans.options)
            if kb and self.last_final_id is not None:
                self.last_final_markup = kb
        await self._sync_title()
        if self._jsonl().exists():
            self.mirror_offset = self._jsonl().stat().st_size
        self.turn = TurnState()
        if self.last_final_id is not None:
            self.last_final_id = last_id
            if last_body is not None:
                # a chunked turn: the panel rides the last answer chunk, so give the
                # updater that chunk's body (not the full text) — no oversized bad edit
                self.last_final_body = last_body
            self._ensure_background_updater()
        self._save()

    async def _heartbeat(self):
        try:
            while True:
                await asyncio.sleep(EDIT_INTERVAL)
                if not self.busy or self.holder_id is None:
                    continue
                elapsed = int(time.monotonic() - self.turn_start)
                self.spin += 1
                tokens = self.turn.tok_base + self.turn.tok_latest
                head = status_head(self.word_seed, self.spin, elapsed, tokens)
                lines, _, _ = render_timeline(self.turn.timeline, drop_last_text=False)
                thinking = "".join(self.turn.thinking_parts).strip()
                answer = "".join(self.turn.preview_parts).strip()
                if thinking:
                    lines.append(f"🧠 {thinking}")
                if answer:
                    lines.append(f"💬 {answer}")
                interim = "\n\n".join(lines)
                base = f"{head}\n\n{interim}" if interim else head
                background.mark_orphans(self)
                panel = background.panel(self, self.spin)
                room = MAX_MSG - (len(panel[1]) + 8 if panel else 0)
                if len(base) > room:
                    base = f"{head}\n\n…{base[-(room - len(head) - 5) :]}"
                if panel:
                    await self.edit_md(
                        self.holder_id,
                        f"{mdv2_escape(base)}\n\n{panel[0]}",
                        f"{base}\n\n{panel[1]}",
                    )
                else:
                    await self.edit(self.holder_id, base)
        except asyncio.CancelledError:
            pass
        except Exception:
            LOGGER.exception("heartbeat error (%s)", self.name)

    async def _attach_suggestions(self, message_id, options):
        token = uuid.uuid4().hex[:8]
        self._suggested[token] = {"options": options}
        while len(self._suggested) > 20:  # bounded — only recent turns' buttons stay live
            self._suggested.pop(next(iter(self._suggested)))
        kb = ui.suggestion_buttons(options, token)
        await self.set_markup(message_id, kb)
        return kb

    @action("sug")
    async def _on_suggestion(self, ctx, arg):
        token, _, idx = arg.partition(":")
        info = self._suggested.pop(token, None)
        if ctx.message is not None:
            await self.set_markup(ctx.message.message_id, None)  # clear the tapped/dead button
        if info and idx.isdigit():
            option = info["options"][int(idx)]
            # record the pick as a visible message so the tap is confirmed even if
            # routing fails; the turn then replies to it (Telegram won't post as the user)
            recorded = await self.send(f"☑️ {option}")
            await self._core.route_as_user(self.thread_id, option, reply_to=recorded)

    # ── Mirror (non-driven sessions from the transcript) ───────────
    async def _mirror_loop(self):
        while True:
            await asyncio.sleep(MIRROR_INTERVAL)
            if self.chat_id is None or self.busy or self.owned:
                continue
            try:
                if await self._mirror_once():
                    self._save()
            except Exception:
                LOGGER.exception("mirror failed (%s)", self.name)

    async def _mirror_once(self) -> bool:
        jsonl = self._jsonl()
        try:
            size = jsonl.stat().st_size
        except OSError:
            return False
        if self.mirror_offset < 0:
            self.mirror_offset = size
            return True
        if size <= self.mirror_offset:
            return False
        with open(jsonl, "rb") as f:
            f.seek(self.mirror_offset)
            chunk = f.read(size - self.mirror_offset)
        if not chunk.endswith(b"\n"):
            last_nl = chunk.rfind(b"\n")
            if last_nl < 0:
                return False
            chunk = chunk[: last_nl + 1]
        self.mirror_offset += len(chunk)
        texts, tools, images, in_flight = mirror_parse(chunk.decode("utf-8", errors="replace"))
        for text in texts:
            await self.send_rich(text)
        for media_type, b64 in images:
            await self.send_photo(media_type, b64)
        await self._mirror_status(tools, in_flight)
        await self._sync_title()
        return True

    async def _mirror_status(self, tools, in_flight):
        if tools:
            self.mirror_tools += len(tools)
            self.mirror_recent = (self.mirror_recent + tools)[-5:]
        if in_flight:
            now = time.time()
            if self.mirror_holder is None:
                self.mirror_start = now
                self.mirror_holder = await self.send("working…")
            self.mirror_tick += 1
            frame = "✶✸✹✺✹✸"[self.mirror_tick % 6]
            word = WORDS[(self.thread_id + self.mirror_tick // 8) % len(WORDS)]
            elapsed = int(now - self.mirror_start)
            n = self.mirror_tools
            plural = "s" if n != 1 else ""
            head = f"{frame} {word}… ({fmt_duration(elapsed)} · {n} tool call{plural})"
            body = "\n".join(self.mirror_recent) or "thinking"
            await self.edit(self.mirror_holder, f"{head}\n{body}")
        elif self.mirror_holder is not None:
            await self.delete(self.mirror_holder)
            self.mirror_holder = None
            self.mirror_tools = 0
            self.mirror_recent = []
            self.mirror_tick = 0

    # ── Title ──────────────────────────────────────────────────────
    def _read_ai_title(self) -> str | None:
        jsonl = self._jsonl()
        if not jsonl.exists():
            return None
        ai = custom = None
        for line in jsonl.read_text().splitlines():
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ev.get("type") == "ai-title":
                ai = ev.get("aiTitle") or ai
            elif ev.get("type") == "custom-title":
                custom = ev.get("customTitle") or custom
        return custom or ai

    def _write_title(self, title):
        jsonl = self._jsonl()
        if not jsonl.exists():
            return
        with open(jsonl, "a") as f:
            f.write(
                json.dumps(
                    {
                        "type": "custom-title",
                        "customTitle": title,
                        "sessionId": self.session_id,
                    }
                )
                + "\n"
            )

    async def _sync_title(self):
        title = self._read_ai_title()
        if not title or title == self.title:
            return
        self.title = title
        self.saved["base_name"] = title  # the session title becomes the topic name
        self._save()
        await self.refresh_title()

    async def _generate_title(self) -> str | None:
        excerpt = self._transcript_excerpt()
        if not excerpt:
            return None
        proc = await self.spawn(
            self.plugin.claude_bin,
            "-p",
            # cheap model + tiny inline system prompt: skips loading the full
            # Claude Code system prompt + tool schemas that a default one-shot
            # would re-send (and re-bill) uncached on every title.
            "--model",
            "claude-haiku-4-5",
            "--system-prompt",
            "You write short conversation titles. Reply with the title only, "
            "at most 8 words, no quotes.",
            "Write a title for this conversation:\n\n" + excerpt,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            cwd=self.workspace,
            env=self._login_env(self._session_config_dir()),
        )
        out, _ = await proc.communicate()
        if proc.returncode != 0:
            return None
        lines = [ln.strip() for ln in out.decode(errors="replace").splitlines() if ln.strip()]
        return lines[0].strip('"') if lines else None

    def _transcript_excerpt(self, limit: int = 4000) -> str:
        jsonl = self._jsonl()
        if not jsonl.exists():
            return ""
        parts: list[str] = []
        for line in jsonl.read_text().splitlines():
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            msg = ev.get("message")
            if not isinstance(msg, dict):
                continue
            content = msg.get("content")
            if isinstance(content, str):
                parts.append(content)
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        parts.append(block.get("text", ""))
        return "\n".join(parts)[-limit:]

    # ── Media routing ──────────────────────────────────────────────
    def _attach_dir(self) -> Path:
        return Path(self.workspace) / "tmp" / "telegram"

    async def _route_media(self, message, text):
        gid = message.media_group_id
        if not gid:
            built = await self._build_prompt([message], text)
            if built:
                await self.submit(built, reply_to=message.message_id)
            return
        buf = self._albums.setdefault(gid, {"msgs": [], "text": ""})
        buf["msgs"].append(message)
        if text and not buf["text"]:
            buf["text"] = text
        old = buf.get("task")
        if old is not None:
            old.cancel()
        buf["task"] = asyncio.create_task(self._flush_album(gid))

    async def _flush_album(self, gid):
        try:
            await asyncio.sleep(ALBUM_DEBOUNCE)
        except asyncio.CancelledError:
            return
        buf = self._albums.pop(gid, None)
        if not buf or not buf["msgs"]:
            return
        prompt = await self._build_prompt(buf["msgs"], buf["text"])
        await self.submit(prompt, reply_to=buf["msgs"][0].message_id)

    def _context_prefix(self, message) -> str:
        notes: list[str] = []
        fo = getattr(message, "forward_origin", None)
        fname = None
        if fo is not None:
            user = getattr(fo, "sender_user", None)
            chat = getattr(fo, "sender_chat", None) or getattr(fo, "chat", None)
            fname = (
                getattr(user, "full_name", None)
                or getattr(fo, "sender_user_name", None)
                or getattr(chat, "title", None)
                or "an unknown source"
            )
        if fname:
            notes.append(f"[Forwarded message from {fname}]")
        reply = getattr(message, "reply_to_message", None)
        if reply is not None and not getattr(reply, "forum_topic_created", None):
            rtext = (reply.text or reply.caption or "").strip()
            has_file = bool(reply.photo or reply.document)
            if rtext or has_file:
                quoted = rtext or "(attachment)"
                if len(quoted) > 500:
                    quoted = quoted[:497] + "..."
                tail = " [with attachment]" if has_file and rtext else ""
                who = reply.from_user.full_name if reply.from_user else "someone"
                notes.append(f"[Replying to {who}: {quoted}{tail}]")
        return "\n".join(notes)

    async def _build_prompt(self, msgs, text) -> str:
        parts: list[str] = []
        ctx = self._context_prefix(msgs[0])
        if ctx:
            parts.append(ctx)
        if text:
            parts.append(text)
        sources = list(msgs) + [getattr(msgs[0], "reply_to_message", None)]
        paths: list[Path] = []
        for src in sources:
            paths += await self._download_attachments(src)
        if paths:
            listing = "\n".join(str(p) for p in paths)
            parts.append(f"[Attached file(s), saved locally — Read to view:\n{listing}\n]")
        return "\n\n".join(parts).strip()

    async def _download_attachments(self, message) -> list[Path]:
        out: list[Path] = []
        if message is None:
            return out
        if message.photo:
            dest = self._attach_dir() / f"{uuid.uuid4().hex}.jpg"
            p = await self._core.download_file(message.photo[-1].file_id, dest)
            if p:
                out.append(p)
        if message.document:
            safe = Path(message.document.file_name).name if message.document.file_name else ""
            dest = self._attach_dir() / (
                f"{uuid.uuid4().hex[:8]}__{safe}" if safe else f"{uuid.uuid4().hex}.bin"
            )
            p = await self._core.download_file(message.document.file_id, dest)
            if p:
                out.append(p)
        return out

    # ── Accounts + pickers ─────────────────────────────────────────
    def _account_name(self, config_dir: Path) -> str:
        base = config_dir.name.lstrip(".")
        if base.startswith("claude-"):
            base = base[len("claude-") :]
        return base or "default"

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
            # the live turn is still writing the old account's jsonl; carrying it now
            # would drop the turn's tail. Switch once the turn settles.
            await self.send("busy — /cancel first, then switch account")
            return
        cd = self.plugin.claude_dir
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
        self.plugin.accounts[name] = str(choice)
        self.plugin._save()

    def _switch_account(self, old_dir: Path, new_dir: Path, config_dir: str | None):
        """Point this window at another account, carrying its transcript so history
        survives (else the new dir has no jsonl → a fresh empty session), and dropping an
        idle process so the next message respawns under the new account."""
        self._carry_transcript(old_dir, new_dir)
        self.config_dir = config_dir
        self._save()
        if self.proc is not None and self.proc.returncode is None and not self.busy:
            self.proc.kill()  # idle: next message respawns (and --resume finds the carried jsonl)

    async def _pick_model_effort(self):
        if self.plugin.models:  # only ask when the bot configured a choice
            model = await self.menu(
                "🧠 Model",
                [(label, value) for label, value in self.plugin.models]
                + [("Default", "__default__")],
            )
            if model and model != "__default__":
                self.model = model
        effort = await self.menu(
            "⚡ Effort",
            [("low", "low"), ("medium", "medium"), ("high", "high"), ("default", "__default__")],
        )
        if effort and effort != "__default__":
            self.effort = effort
        self._save()

    # ── /switch (account / model / effort mid-session) ─────────────
    def _account_label(self, config_dir: str | None) -> str:
        if config_dir is None:
            return f"default ({self._account_name(self.plugin.claude_dir)})"
        return self._account_name(Path(config_dir))

    def _carry_transcript(self, old_dir: Path, new_dir: Path):
        if old_dir == new_dir:
            return
        src = _project_dir(old_dir, Path(self.workspace)) / f"{self.session_id}.jsonl"
        dst = _project_dir(new_dir, Path(self.workspace)) / f"{self.session_id}.jsonl"
        if not src.exists():
            return
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)

    async def _interrupt(self):
        if not (self.proc and self.proc.returncode is None):
            return
        ctrl = {
            "type": "control_request",
            "request_id": uuid.uuid4().hex[:8],
            "request": {"subtype": "interrupt"},
        }
        try:
            self.proc.stdin.write((json.dumps(ctrl) + "\n").encode())
            await self.proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError, RuntimeError):
            pass
        self.cancel_requested = True

    # ── Class commands ─────────────────────────────────────────────
    @command("/stop", "close this window (session stays on disk)", icon="⏹")
    async def stop(self, ctx):
        choice, _ = await self.ask_buttons(
            f"Close topic '{self.name}'? The session stays on disk, resumable with /cli.",
            [("Yes, close", "yes")],
        )
        if choice != "yes":
            await self.send("kept")
            return
        await self.close()

    @command("/cancel", "interrupt the running turn", icon="✖️")
    async def cancel(self, ctx):
        if self.busy and self.proc and self.proc.returncode is None:
            await self._interrupt()
            await self.send("cancelling...")

    def _fresh_session(self) -> str:
        """Reset to a brand-new session id (old transcript stays resumable); returns
        the old id. The next turn respawns the CLI against the new session."""
        old = self.session_id
        self.session_id = str(uuid.uuid4())
        self.mirror_offset = -1
        self.mirror_holder = None
        self.holder_id = None
        self.pending_writes = []
        self.turn = TurnState()
        return old

    @command("/clear", "start a fresh session (old one stays resumable)", icon="🧹")
    async def clear(self, ctx):
        if self.busy:
            await self.send("busy — /cancel first, then /clear")
            return
        old = self._fresh_session()
        self._save()
        await self.send(f"cleared — fresh session {self.session_id[:8]} (old {old[:8]} resumable)")

    @command("/cli", "resume this session on the PC", icon="💻")
    async def cli(self, ctx):
        cfg = str(self._session_config_dir())
        line = f"cd {self.workspace} && CLAUDE_CONFIG_DIR={cfg} claude --resume {self.session_id}"
        await self.send(f"pick up on the PC:\n{line}")

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
        await self.refresh_title()

    @command("/account", "switch this topic's Claude account", icon="👤")
    async def account(self, ctx):
        await self._pick_account()

    @command("/mode", "set the Claude permission mode (app-wide)", icon="🔐")
    async def mode(self, ctx):
        cur = self.plugin.permission_mode
        choice = await self.menu(
            f"🔐 Permission mode (now: {cur})",
            [(f"{m} ✓" if m == cur else m, m) for m in PERMISSION_MODES],
        )
        if not choice or choice == cur:
            return
        self.plugin.set_permission_mode(choice)
        await self.send(f"permission mode → {choice}")
        if self.proc is not None and self.proc.returncode is None and not self.busy:
            self.proc.kill()  # idle: next message respawns with the new mode

    @command("/status", "show this session's state", icon="🗒", inline=True)
    async def status(self, ctx):
        return (
            f"{self.name} · {'busy' if self.busy else 'idle'} · "
            f"{self._account_label(self.config_dir)} · "
            f"model {self.model or self.plugin.default_model or 'default'} · "
            f"mode {self.plugin.permission_mode}"
        )

    @command("/models", "edit the model list offered at /claude", icon="🧠")
    async def models(self, ctx):
        await self.plugin.models_cmd(ctx, self)

    @command("/workspaces", "switch, add, or remove workspace roots", icon="📁")
    async def workspaces(self, ctx):
        if ctx.args:  # typed add/rm keeps working from the keyboard
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
        self.workspace = choice
        self._fresh_session()  # a new cwd is a new session (new project dir)
        self._save()
        await self.send(f"📁 workspace · {Path(choice).name} — fresh session {self.session_id[:8]}")

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
        await self._route_media(ctx.message, ctx.text)

    @on_unknown
    async def skill(self, ctx):
        await self.submit(ctx.text, reply_to=getattr(ctx.message, "message_id", None))


class Claude(Plugin):
    id = "claude"
    topics = [ClaudeTopic]

    def __init__(self, roots=None, model=None, models=None, brief=None, permission_mode=None):
        self._init_roots = list(roots) if roots else []
        self._init_model = model
        self._init_models = models
        self._init_permission_mode = permission_mode or DEFAULT_PERMISSION_MODE
        self.brief = brief  # app-supplied extra guidance; None → try the bot-home file
        self.brief_resolved: str | None = None
        self.claude_dir = claude_dir()
        self.claude_bin = claude_bin()
        self.default_model = model
        self.permission_mode = self._init_permission_mode  # --permission-mode on every spawn
        self.accounts: dict[str, str] = {}
        self.models: list[list[str]] = [list(m) for m in (models or DEFAULT_MODELS)]
        self.roots: list[str] = list(self._init_roots)
        self.workspaces = resolve_roots(self.roots)

    def _load(self):
        self.default_model = self.saved.get("model", self._init_model)
        self.permission_mode = self.saved.get("permission_mode", self._init_permission_mode)
        self.accounts = self.saved.get("accounts", {})
        fallback = self._init_models or DEFAULT_MODELS
        self.models = self.saved.get("models") or [list(m) for m in fallback]
        self.roots = self.saved.get("roots") or list(self._init_roots)
        self.workspaces = resolve_roots(self.roots)

    def _save(self):
        self.saved["model"] = self.default_model
        self.saved["permission_mode"] = self.permission_mode
        self.saved["accounts"] = self.accounts
        self.saved["models"] = self.models
        self.saved["roots"] = self.roots

    def set_permission_mode(self, mode: str) -> None:
        self.permission_mode = mode
        self._save()

    async def on_startup(self):
        setup.install_claude_as()
        setup.install_shell_wrapper()
        self._load()
        self.seed_accounts()

    # ── Accounts ───────────────────────────────────────────────────
    def scan_accounts(self) -> dict[str, str]:
        found = {}
        try:
            for sib in sorted(self.claude_dir.parent.iterdir()):
                if sib.is_dir() and (sib / ".credentials.json").exists():
                    name = sib.name.lstrip(".")
                    if name.startswith("claude-"):
                        name = name[len("claude-") :]
                    found.setdefault(name or "default", str(sib))
        except OSError:
            pass
        return found

    def seed_accounts(self):
        before = dict(self.accounts)
        for name, d in self.scan_accounts().items():
            self.accounts.setdefault(name, d)
        if self.accounts != before:
            self._save()

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
        cfg = self.claude_dir.parent / f".claude-{name}"
        if not await self._run_login(topic, name, cfg):
            return None
        self.accounts[name] = str(cfg)
        self._save()
        return name, str(cfg)

    async def _run_login(self, io, name: str, cfg: Path) -> bool:
        """Run `claude auth login --claudeai` into `cfg` (new or existing). `io` is a
        topic or ctx — anything with send/ask_text. True on a credentialed result."""
        cfg.mkdir(parents=True, exist_ok=True)
        await io.send(f"starting login for '{name}'…")
        env = {
            **os.environ,
            "PATH": f"{Path.home()}/.local/bin:" + os.environ.get("PATH", ""),
            "CLAUDE_CONFIG_DIR": str(cfg),
            "BROWSER": "true",
        }
        env.pop("DISPLAY", None)
        env.pop("WAYLAND_DISPLAY", None)
        proc = await asyncio.create_subprocess_exec(
            self.claude_bin,
            "auth",
            "login",
            "--claudeai",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
        )
        buf = ""
        try:
            while "http" not in buf:
                chunk = await asyncio.wait_for(proc.stdout.read(2048), timeout=25)
                if not chunk:
                    break
                buf += chunk.decode(errors="replace")
        except TimeoutError:
            pass
        if "http" not in buf:
            await reap(proc)
            await io.send(f"login produced no URL:\n{buf[-800:]}")
            return False
        m = re.search(r"https?://\S+", buf)
        url = m.group(0) if m else buf.strip()
        code = await io.ask_text(
            f"Open this to sign in, then paste the code back here:\n{url}", timeout=300
        )
        if not code:
            await reap(proc)
            await io.send("login timed out — cancelled")
            return False
        try:
            proc.stdin.write((code.strip() + "\n").encode())
            await proc.stdin.drain()
            out = await asyncio.wait_for(proc.stdout.read(), timeout=60)
            rc = await asyncio.wait_for(proc.wait(), timeout=15)
        except (TimeoutError, BrokenPipeError, ConnectionResetError, OSError):
            await reap(proc)
            await io.send("login: failed to complete")
            return False
        if not (cfg / ".credentials.json").exists():
            tail = out.decode(errors="replace")[-800:]
            await io.send(f"login didn't complete [exit {rc}]:\n{tail}")
            return False
        return True

    def account_status(self) -> list[dict]:
        """Per-account credential state from each dir's .credentials.json (claudeAiOauth)."""
        now_ms = int(time.time() * 1000)
        # drop any scan/known entry pointing at the default dir; it gets one "default" row
        accounts = {
            name: d
            for name, d in {**self.accounts, **self.scan_accounts()}.items()
            if Path(d) != self.claude_dir
        }
        accounts["default"] = str(self.claude_dir)
        rows: list[dict] = []
        for name, cfg_dir in sorted(accounts.items()):
            creds = Path(cfg_dir) / ".credentials.json"
            expired = False
            if not creds.exists():
                label, expired = f"❌ {name} — no credentials", True
            else:
                try:
                    oauth = json.loads(creds.read_text()).get("claudeAiOauth", {})
                except (OSError, ValueError):
                    oauth = None
                if oauth is None:
                    label, expired = f"⚠️ {name} — error reading creds", True
                else:
                    sub = oauth.get("subscriptionType", "?")
                    exp = oauth.get("expiresAt", 0)
                    rexp = oauth.get("refreshTokenExpiresAt", 0)
                    if rexp and rexp < now_ms:
                        label, expired = f"❌ {name} — {sub} — refresh token expired", True
                    elif exp and exp < now_ms:
                        label = f"🔄 {name} — {sub} — access token expired (refresh OK)"
                    else:
                        days = (rexp - now_ms) / 86_400_000 if rexp else 0
                        label = f"✅ {name} — {sub} — OK ({days:.0f}d)"
            rows.append({"name": name, "dir": cfg_dir, "label": label, "expired": expired})
        return rows

    @universal("/login", "credential status; re-login expired", icon="🔑")
    async def login_cmd(self, ctx):
        rows = self.account_status()
        await ctx.send("account login status:\n" + "\n".join(r["label"] for r in rows))
        expired = [r for r in rows if r["expired"]]
        if not expired:
            return
        options = [(r["name"], r["dir"]) for r in expired]
        options.append(("➕ Log in new account", "__new__"))
        choice = await ctx.menu("🔑 Re-login an account?", options)
        if not choice:
            return
        if choice == "__new__":
            await self.login_new_account(ctx)
            return
        cfg = Path(str(choice))
        name = next((r["name"] for r in expired if r["dir"] == str(choice)), cfg.name)
        if await self._run_login(ctx, name, cfg):
            self.accounts[name] = str(cfg)
            self._save()
            await ctx.send(f"✅ '{name}' re-logged in")

    # ── Model-list edits (shared by the typed path + the /models menu) ─
    def add_model(self, label: str, value: str) -> None:
        self.models.append([label, value])
        self._save()

    def remove_model(self, idx: int) -> list | None:
        if 0 <= idx < len(self.models):
            removed = self.models.pop(idx)
            self._save()
            return removed
        return None

    def reset_models(self) -> None:
        self.models = [list(m) for m in DEFAULT_MODELS]
        self._save()

    def models_text(self) -> str:
        lines = ["models offered at /claude:"]
        lines += [f"  {i}. {label} — {value}" for i, (label, value) in enumerate(self.models, 1)]
        return "\n".join(lines)

    # ── Workspace-root edits (shared by the typed path + the menu) ─────
    def add_root(self, glob_str: str) -> int:
        self.roots.append(glob_str)
        self.workspaces = resolve_roots(self.roots)
        self._save()
        return len(self.workspaces)

    def remove_root(self, idx: int) -> str | None:
        if 0 <= idx < len(self.roots):
            removed = self.roots.pop(idx)
            self.workspaces = resolve_roots(self.roots)
            self._save()
            return removed
        return None

    def roots_text(self) -> str:
        lines = ["workspace roots (globs):"]
        lines += [f"  {i}. {g}" for i, g in enumerate(self.roots, 1)] or ["  (none)"]
        lines += ["", "resolved:"]
        lines += [f"  • {w}" for w in self.workspaces]
        return "\n".join(lines)

    # ── /models + /workspaces (list editors, run from a claude window) ─
    async def models_cmd(self, ctx, topic):
        if ctx.args:  # typed add/rm/reset keeps working from the keyboard
            await self._models_typed(ctx)
            return
        opts = [("➕ Add a model", "add")]
        if self.models:
            opts.append(("➖ Remove a model", "remove"))
        opts += [("↩️ Reset to defaults", "reset"), ("📋 List models", "list")]
        choice = await ctx.menu("🧠 Models", opts)
        if choice == "add":
            raw = await ctx.ask_text("Send the model as `label | model-id` (or just the id):")
            if raw and raw.strip():
                label, _, value = raw.strip().partition("|")
                label, value = label.strip(), (value.strip() or label.strip())
                self.add_model(label, value)
                await ctx.send(f"added '{label}' → {value}")
        elif choice == "remove":
            pick = await ctx.menu(
                "➖ Remove a model",
                [(f"{label} — {value}", str(i)) for i, (label, value) in enumerate(self.models)],
            )
            if pick is not None:
                removed = self.remove_model(int(pick))
                if removed:
                    await ctx.send(f"removed '{removed[0]}'")
        elif choice == "reset":
            self.reset_models()
            await ctx.send("model list reset to defaults")
        elif choice == "list":
            await ctx.send(self.models_text())

    async def _models_typed(self, ctx):
        parts = ctx.args.split(maxsplit=1)
        sub = parts[0].lower()
        rest = parts[1].strip() if len(parts) > 1 else ""
        if sub == "add" and rest:
            label, _, value = rest.partition("|")
            label, value = label.strip(), (value.strip() or label.strip())
            self.add_model(label, value)
            await ctx.send(f"added '{label}' → {value}")
        elif sub in ("rm", "remove", "del") and rest:
            idx = None
            if rest.isdigit():
                idx = int(rest) - 1
            else:
                idx = next((j for j, (la, va) in enumerate(self.models) if rest in (la, va)), None)
            removed = self.remove_model(idx) if idx is not None else None
            await ctx.send(f"removed '{removed[0]}'" if removed else f"no model matching '{rest}'")
        elif sub == "reset":
            self.reset_models()
            await ctx.send("model list reset to defaults")
        else:
            await ctx.send(self.models_text())

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

    # ── One-shot shell prefixes (claude's ! / !!) ──────────────────
    @prefix("!!", "run a shell command and hand the result to the agent")
    async def bang_bang(self, ctx):
        return await self._one_shot(ctx, chain=True)

    @prefix("!", "run a one-shot shell command in the agent's workspace")
    async def bang(self, ctx):
        return await self._one_shot(ctx, chain=False)

    async def _one_shot(self, ctx, chain):
        if not isinstance(ctx.topic, ClaudeTopic):
            return False  # only inside a claude window; else fall through the chain
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
        status = ""
        proc = None
        try:
            proc = await asyncio.create_subprocess_shell(
                cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=topic.workspace,
                env=env,
            )

            async def pump():
                last = 0.0
                while True:
                    ln = await proc.stdout.readline()
                    if not ln:
                        break
                    buf.append(ln.decode(errors="replace"))
                    now = time.monotonic()
                    if hid is not None and now - last >= EDIT_INTERVAL:
                        last = now
                        await topic.edit_md(hid, shell_md(cmd, buf, ""), shell_view(cmd, buf, "…"))

            await asyncio.wait_for(pump(), timeout=SHELL_TIMEOUT)
            await proc.wait()
            status = f"exit {proc.returncode}"
        except TimeoutError:
            if proc is not None:
                proc.kill()
                await proc.wait()
            status = f"timed out after {SHELL_TIMEOUT}s"
        except Exception as exc:
            status = f"failed to run: {exc}"
        finally:
            if proc is not None:
                await reap(proc)  # never leave the shell child running on any exit path
        result = shell_view(cmd, buf, status)
        if hid is not None:
            await topic.edit_md(hid, shell_md(cmd, buf, status), result)
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
        if isinstance(ctx.topic, ClaudeTopic):
            await ctx.topic.submit(text)
        else:
            await ctx.send("open a /claude window to hand this to the agent")
