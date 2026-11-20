"""AgyTopic + Agy plugin: the agy (Antigravity CLI) backend of the agent window.

Each `/agy` window is one `AgyTopic`, an `AgentTopic` (`tgforge.plugins.agent`) whose
backend runs one `agy acp` process per window, one ACP session in it, over JSON-RPC
(`acp.py`). ACP updates and the session's transcript turn into the core's neutral
events (`backend.py`); the turn-boundary rules are listed there.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import mimetypes
import os
import pty
import re
import shutil
import time
from pathlib import Path

from tgforge.base.kernel import (
    command_processes,
    kill_processes,
    launch,
    notice_exception,
    process_tree,
    reap,
)
from tgforge.plugins.agent import background as jobs
from tgforge.plugins.agent.session import (
    PromptFolded,
    Title,
    TokenTotal,
    ToolEnd,
    Usage,
    apply,
    last_text,
)
from tgforge.plugins.agent.window import (
    TURN_IDLE,
    AgentPlugin,
    AgentTopic,
    attached_files,
    system_prompt_file,
)
from tgforge.plugins.agy import backend, setup
from tgforge.plugins.agy.acp import AcpClient
from tgforge.plugins.agy.config import (
    ACCOUNT_PREFIX,
    DEFAULT_MODELS,
    TOKEN_FILE,
    accounts_root,
    agy_bin,
    data_dir,
    gemini_dir,
)

LOGGER = logging.getLogger("tgforge")

# `/mode` values: ACP's session modes, plus `skip` (a respawn with
# `--dangerously-skip-permissions`). The app default is `auto`.
SKIP_MODE = "skip"
PERMISSION_MODES = ("auto", "default", "accept-edits", "plan", SKIP_MODE)
DEFAULT_PERMISSION_MODE = "auto"
EFFORTS = ("low", "medium", "high", "xhigh")  # ACP's `thought_level` values
# the transcript poll: the interval the wake-turn measurement watched the file at
TRANSCRIPT_POLL = 0.25
LOGIN_WINDOW = 60  # seconds agy's sign-in waits for its code
IMAGE_TYPES = ("image/png", "image/jpeg", "image/gif", "image/webp")
# ours: how long a `/usage` run may take (it does no model work; the captured run reports
# 0 s) before the poll is stopped and counted as failed
QUOTA_POLL_TIMEOUT = 30.0
# ours: how long `session/close` may take to answer before the window kills the process
# (the captured session/new and session/load answered in 1.5-1.7 s)
CLOSE_TIMEOUT = 5.0
QUOTA_POLL_INTERVAL = 60.0  # seconds between two `/usage` polls of one account (user-set)


@launch("/agy", "open an agy agent window")
class AgyTopic(AgentTopic):
    id = "agy"
    icon = "🛸"
    menu_label = "agy"
    agent_name = "agy"
    PERMISSION_MODES = PERMISSION_MODES
    EFFORTS = EFFORTS

    def __init__(self, core, window, name, saved):
        super().__init__(core, window, name, saved)
        self.proc: asyncio.subprocess.Process | None = None
        self.acp: AcpClient | None = None
        self.reader_task: asyncio.Task | None = None
        self.transcript_task: asyncio.Task | None = None
        self.stream = backend.StreamState()
        self.prompts: list[str] = []  # the outstanding session/prompt ids, in send order
        self.loading = False  # a session/load runs: its history replay is dropped
        self.applied: dict[str, str] = {}  # config option → the value the session has
        self.spawned_mode: str | None = None  # the permission mode the process runs in
        # the process the window ended: gone for the window at once, though its reader
        # tears down only once the exit is noticed (stdout's end)
        self.ended_proc = None
        self.transcript_offset = 0  # bytes of the transcript the backend has read
        self.agent_title: str | None = None  # the newest title agy gave the session
        self.session_model: str | None = None  # the session's model when the window sets none
        # the session's own defaults: the `model` / `thought_level` values its first
        # session/new or session/load reply carried (kept with the session id)
        self.session_defaults: dict[str, str] = {}
        self.reply_tokens = 0  # output tokens the running turn's prompt replies reported
        # a wake turn's own output tokens: its transcript steps before the first prompt
        # folded into it (that prompt's `USER_INPUT` step); they get no reply, so the
        # replies' sum leaves them out
        self.wake_tokens = 0
        self.wake_counting = False

    # ── Paths ──────────────────────────────────────────────────────
    def _data_dir(self, account_home: Path | None = None) -> Path:
        return data_dir(account_home or self._session_config_dir())

    def _transcript_path(self) -> Path | None:
        if not self.session_id:
            return None
        brain = self._data_dir() / "brain" / self.session_id
        return brain / ".system_generated" / "logs" / "transcript.jsonl"

    def _mirror_parse(self, raw: str):
        return backend.mirror_parse(raw)

    def _new_session_id(self) -> None:
        return None  # agy assigns it at session/new

    def _account_env(self) -> str:
        """The `HOME=` prefix a PC command needs for this window's account."""
        return f"HOME={self.config_dir} " if self.config_dir else ""

    def _cli_line(self) -> str:
        return f"cd {self.workspace} && {self._account_env()}agy --conversation {self.session_id}"

    def _carry_transcript(self, old_dir: Path, new_dir: Path) -> None:
        """Copy the session's database and brain dir to the new account's data dir."""
        if not self.session_id:
            return
        src, dst = data_dir(old_dir), data_dir(new_dir)
        db = src / "conversations" / f"{self.session_id}.db"
        if db.exists():
            (dst / "conversations").mkdir(parents=True, exist_ok=True)
            shutil.copy2(db, dst / "conversations" / db.name)
        brain = src / "brain" / self.session_id
        if brain.is_dir():
            shutil.copytree(brain, dst / "brain" / self.session_id, dirs_exist_ok=True)

    async def _generate_title(self) -> str | None:
        return self.agent_title

    def _quota(self) -> dict | None:
        """The quota of the group naming the window's model (by its display name)."""
        groups = self.plugin.usage_groups.get(self._account_key())
        if not groups:
            return None
        model = self._model_id() or self.session_model
        return backend.model_quota(groups, self.plugin.model_names.get(model))

    def _note_options(self, result) -> None:
        """Keep the model options and the current model a session reply carries."""
        current = self.plugin.offer_models(result or {})
        if current:
            self.session_model = current

    def _note_defaults(self, result) -> None:
        """Keep the session's defaults from its first session/new or session/load reply:
        a session already known keeps the ones it started with."""
        kept = self.saved.get("agy_defaults") or {}
        if kept.get("session") == self.session_id:
            self.session_defaults = dict(kept.get("values") or {})
            return
        self.session_defaults = {
            option["id"]: option["currentValue"]
            for option in (result or {}).get("configOptions") or []
            if option.get("id") in ("model", "thought_level") and option.get("currentValue")
        }
        self.saved["agy_defaults"] = {"session": self.session_id, "values": self.session_defaults}

    def _held_jobs(self) -> list[str]:
        """The labels of the running jobs this window's live process holds (its end
        would take them down)."""
        if not self.backend_alive():
            return []
        return [t["label"] for t in self.background_tasks.values() if t["done"] is None]

    def _running_mode(self) -> str:
        """`skip` is a launch flag, so a process kept for its job keeps its side of a
        `skip` switch until it is respawned; every other mode switches live, and a busy
        turn with no job ends its process, so the switch applies from the next turn."""
        app = self.plugin.permission_mode
        if not self._held_jobs() or (SKIP_MODE not in (app, self.spawned_mode)):
            return app
        return self.applied.get("mode") or self.spawned_mode or app  # skip sets no option

    # ── The session note ───────────────────────────────────────────
    def _system_append(self) -> str:
        """What the session's system prompt gets appended: the workspace's
        system_prompt.txt (ACP appends only), then the session note."""
        note = self._session_note()
        prompt_file = system_prompt_file(Path(self.workspace))
        if prompt_file is None:
            return note
        return f"{prompt_file.read_text().strip()}\n\n{note}"

    # ── Backend boundary ───────────────────────────────────────────
    def backend_alive(self) -> bool:
        return (
            self.proc is not None
            and self.proc is not self.ended_proc
            and self.proc.returncode is None
            and self.acp is not None
        )

    async def backend_start(self) -> None:
        if self.backend_alive():
            if not self.busy:
                await self._sync_options()
            return
        old_reader = self.reader_task
        if old_reader is not None and old_reader is not asyncio.current_task():
            # the old process's teardown clears the window's turn state: it must finish
            # before this prompt opens its turn (it ends once the exit is noticed)
            await asyncio.wait({old_reader})
        try:
            await self._spawn()
        except (RuntimeError, OSError) as error:  # RuntimeError: an AcpError reply
            if self.proc is not None:
                await reap(self.proc)
            raise RuntimeError(f"agy acp: {error}") from error

    async def _spawn(self) -> None:
        if self.reader_task is not None and not self.reader_task.done():
            self.reader_task.cancel()
        if self.heartbeat_task is not None:
            self.heartbeat_task.cancel()
        mode = self.plugin.permission_mode
        cmd = [self.plugin.agy_bin, "acp"]
        if self.config_dir:
            cmd += ["--gemini-dir", str(gemini_dir(Path(self.config_dir)))]
        if mode == SKIP_MODE:
            cmd.append("--dangerously-skip-permissions")
        env = {
            **os.environ,
            "PATH": f"{Path.home()}/.local/bin:" + os.environ.get("PATH", ""),
            "TGFORGE_WINDOW": self.window,
            "TGFORGE_BOT_DRIVEN": "1",
        }
        self.proc = await self.spawn(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            cwd=self.workspace,
            env=env,
            limit=16 * 1024 * 1024,
        )
        self.spawned_mode = mode
        self.acp = AcpClient(self.proc.stdout, self.proc.stdin, self)
        self.stream = backend.StreamState()
        self.prompts = []
        self.applied = {}
        self.owned = True
        self.reader_task = asyncio.create_task(self._reader())
        await self.acp.request("initialize", {"protocolVersion": 1, "clientCapabilities": {}})
        system_append = self._system_append()
        note_hash = hashlib.sha256(system_append.encode()).hexdigest()
        meta = {"antigravity": {"systemPrompt": {"append": system_append}}}
        params = {"cwd": self.workspace, "mcpServers": []}
        db = self._data_dir() / "conversations" / f"{self.session_id}.db"
        if self.session_id and db.exists():
            load = {"sessionId": self.session_id, **params}
            if self.saved.get("note_hash") != note_hash:  # the note changed: resent
                load["_meta"] = meta
            self.loading = True
            try:
                result = await self.acp.request("session/load", load)
            finally:
                self.loading = False
        else:
            result = await self.acp.request("session/new", {**params, "_meta": meta})
            self.session_id = result["sessionId"]
            self.mirror_offset = -1
        self.saved["note_hash"] = note_hash
        self._note_defaults(result)
        self._note_options(result)
        self._save()
        await self._sync_options()
        path = self._transcript_path()
        self.transcript_offset = path.stat().st_size if path.exists() else 0
        self.transcript_task = asyncio.create_task(self._transcript_loop())
        self._arm_heartbeat()
        LOGGER.info("agy acp spawn (%s): session %s", self.name, self.session_id)

    async def _sync_options(self) -> None:
        """Bring the session's model, thought level and mode to the window's."""
        defaults = self.session_defaults  # a window set to default: the session's own
        wanted = {
            "model": self._model_id() or defaults.get("model", ""),
            "thought_level": self.effort or defaults.get("thought_level", ""),
        }
        if SKIP_MODE not in (self.spawned_mode, self.plugin.permission_mode):
            wanted["mode"] = self.plugin.permission_mode  # a skip switch waits for a respawn
        for config_id, value in wanted.items():
            if value and self.applied.get(config_id) != value:
                result = await self.acp.request(
                    "session/set_config_option",
                    {"sessionId": self.session_id, "configId": config_id, "value": value},
                )
                self.applied[config_id] = value
                self._note_options(result)

    async def backend_prompt(self, text: str) -> str | None:
        blocks: list[dict] = [{"type": "text", "text": text}]
        for path in attached_files(text):  # an image also goes as an image block
            mime = mimetypes.guess_type(path.name)[0]
            if mime in IMAGE_TYPES:
                data = base64.b64encode(path.read_bytes()).decode()
                blocks.append({"type": "image", "mimeType": mime, "data": data})
        params = {"sessionId": self.session_id, "prompt": blocks}
        try:
            prompt_id, _ = await self.acp.send_request("session/prompt", params, wait=False)
        except RuntimeError as error:  # AcpError: the process is going; its reader's
            # teardown reports each queued message as not delivered
            LOGGER.warning("agy (%s): prompt not sent: %s", self.name, error)
            return None
        self.prompts.append(prompt_id)
        return prompt_id

    async def backend_cancel(self) -> bool:
        return await self.acp.notify("session/cancel", {"sessionId": self.session_id})

    async def backend_cancelled(self) -> None:
        """A wake turn (no prompt outstanding) gets no reply and no transcript end
        after a cancel: it ends here, as a cancelled turn."""
        if self.turn_open and not self.prompts:
            await self._close_turn("cancelled")

    async def backend_set_option(self, name: str, value: str | None) -> None:
        """A live, idle session takes a model, effort or mode at once; one mid-turn
        takes it before the next turn; a reset to the default (no value) sends the
        session's own default; `skip` (or leaving it) needs a respawn, after the jobs end."""
        if name == "mode" and SKIP_MODE in (value, self.spawned_mode):
            self._respawn_idle()  # a process running a job keeps it: the next spawn's
        elif self.backend_alive() and not self.busy:
            await self._sync_options()  # a reset to default sends the session's own

    def backend_end(self) -> None:
        """End the process; agy takes its jobs' trees down with it (measured), and the
        reader's teardown ends their rows as ◼."""
        self.ended_proc = self.proc
        self.proc.kill()

    async def backend_close(self) -> None:
        if self.backend_alive() and self.session_id:  # a bounded wait for agy to close it
            try:
                await asyncio.wait_for(
                    self.acp.request("session/close", {"sessionId": self.session_id}),
                    CLOSE_TIMEOUT,
                )
            except (TimeoutError, RuntimeError) as error:  # RuntimeError: an AcpError
                LOGGER.info("agy (%s): session/close unanswered: %r", self.name, error)
        for task in (self.reader_task, self.transcript_task):
            if task is not None:
                task.cancel()
        if self.proc:
            await reap(self.proc)

    async def backend_stop_job(self, job_id: str, task: dict) -> None:
        """Kill the job's process tree: the descendant of this window's agy process
        running the tool call's command line; refused when several match."""
        label = task["label"]
        command = self.stream.tools.get(job_id, {}).get("command")
        if not command or not self.backend_alive():
            await self.send(f"can't stop {label} — its agy process is gone")
            return
        matches = command_processes(self.proc.pid, command)
        if matches is None:
            await self.send(f"can't stop {label} — no process probe on this platform")
            return
        if not matches:
            await self.send(f"{label} has no running processes left")
            return
        if len(matches) > 1:
            await self.send(f"refused to stop {label}: {len(matches)} processes run its command")
            return
        task["stopping"] = True
        await self._send_turn_note(f"stopping {label}…")
        await kill_processes(process_tree({matches[0]}))

    # ── ACP messages (the client's handler) ────────────────────────
    async def _reader(self):
        try:
            await self.acp.run()
        except Exception as error:
            LOGGER.exception("agy reader error (%s)", self.name)
            notice_exception(self._core.notice, self.window, "agy reader", error)
        finally:
            if self.transcript_task is not None:
                self.transcript_task.cancel()
                self.transcript_task = None
            # the process is gone: so is every job it ran (its rows end as ◼; the
            # panel's updater, running while a row shows, prunes them)
            ended = [b for b, t in self.background_tasks.items() if t["done"] is None]
            for job_id in ended:
                jobs.end_job(self, job_id, "◼")
            await self._process_exited()
            self.proc = None
            self.acp = None
            self.prompts = []
            self.reader_task = None
            self._save()

    async def on_notification(self, method: str, params: dict) -> None:
        if method != "session/update" or self.loading:
            return  # a session/load's history replay is dropped
        self._note_activity()
        for event in backend.update_events(self.stream, params.get("update") or {}):
            await self._backend_event(event)

    async def _backend_event(self, event) -> None:
        kind = type(event)
        if kind is Usage and not self.turn_open:
            if event.context_size:  # between turns: only the window size is kept
                self.context_window = event.context_size
            return
        if kind is ToolEnd and not self.turn_open:
            return  # a job's tool line belongs to the turn that started it
        if kind is Title:
            self.agent_title = event.text
        elif kind is backend.JobEnded and not self.turn_open:
            await self.on_event(event)  # the job ended between turns: a wake turn opens
            self._drain_transcript()  # steps already written belong to the earlier turn
            self._open_next_turn()
            self.wake_counting = self.turn_open and not self.prompts
            return
        await self.on_event(event)

    async def on_request(self, request_id, method: str, params: dict) -> None:
        if method != "session/request_permission":
            error = {"code": -32601, "message": f"method not supported: {method}"}
            await self.acp.respond(request_id, error=error)
            return
        # the window has no one to ask: every ask is denied (and shown in the timeline)
        options = params.get("options") or []
        deny = next((o["optionId"] for o in options if o.get("kind") == "reject_once"), None)
        deny = deny or next((o["optionId"] for o in options if "reject" in o.get("kind", "")), None)
        tool_id = (params.get("toolCall") or {}).get("toolCallId")
        if tool_id:
            self.stream.denied.add(tool_id)
        if deny is None:
            await self.acp.respond(request_id, {"outcome": {"outcome": "cancelled"}})
        else:
            await self.acp.respond(
                request_id, {"outcome": {"outcome": "selected", "optionId": deny}}
            )

    async def on_reply(self, request_id: str, method: str, result, error) -> None:
        if method != "session/prompt" or request_id not in self.prompts:
            return
        index = self.prompts.index(request_id)
        later = self.prompts[index + 1 :]
        self.prompts.remove(request_id)
        reason = (result or {}).get("stopReason") or "error"
        tokens = ((result or {}).get("usage") or {}).get("outputTokens")
        if isinstance(tokens, int):
            self.reply_tokens += tokens  # every reply of the turn, folded ones included
        if later and error is None and reason == "end_turn":
            self._fold_prompt(request_id)  # its reply came: the prompt is done
            await self.on_event(PromptFolded(later[0]))  # an early reply: the next one joined
            return
        if later:
            return  # an earlier prompt's cancel or error; the turn runs on under the newer
        entry = next((e for e in self.pending_writes if e.get("prompt_id") == request_id), None)
        if entry is not None and not entry["is_initiator"]:
            self._fold_prompt(request_id)  # sent during a wake turn: agy folded it
        subtype = f"error: {error.get('message', '')}" if error else reason
        if self.reply_tokens:  # the replies' count, never short of a late transcript step
            await self.on_event(TokenTotal(self.wake_tokens + self.reply_tokens))
        await self._close_turn(subtype, is_error=error is not None)

    async def _close_turn(self, subtype: str, is_error: bool = False, text: str | None = None):
        """End the running turn: the streamed block closes, the transcript is read up
        to now, every `run_command` still running becomes a job."""
        for event in backend.close_block(self.stream):
            await self.on_event(event)
        self._drain_transcript()
        for event in backend.running_jobs(self.stream):
            await self.on_event(event)
        backend.forget_ended(self.stream)
        answer = text if text is not None else last_text(self.turn)
        self.reply_tokens = 0
        self.wake_tokens = 0
        self.wake_counting = False
        outcome = await self._end_turn(answer, subtype=subtype, is_error=is_error)
        self.plugin.poll_quota(self)
        if outcome == TURN_IDLE:
            self.backend_end()

    # ── Transcript ─────────────────────────────────────────────────
    async def _transcript_loop(self):
        try:
            while True:
                await asyncio.sleep(TRANSCRIPT_POLL)
                for step in self._drain_transcript():
                    end = backend.wake_end(step)
                    if end and self.turn_open and not self.prompts:
                        text = (step.get("content") or "") if end == "end_turn" else ""
                        await self._close_turn(end, is_error=end == "error", text=text)
        except Exception as error:
            LOGGER.exception("agy transcript reader error (%s)", self.name)
            notice_exception(self._core.notice, self.window, "agy transcript reader", error)

    def _drain_transcript(self) -> list[dict]:
        """Read the transcript's new complete lines; their token counts fold into the
        running turn. Returns the new steps."""
        path = self._transcript_path()
        if path is None:
            return []
        try:
            size = path.stat().st_size
        except OSError:
            return []
        if size <= self.transcript_offset:
            return []
        with open(path, "rb") as f:
            f.seek(self.transcript_offset)
            chunk = f.read(size - self.transcript_offset)
        end = chunk.rfind(b"\n")
        if end < 0:
            return []
        self.transcript_offset += end + 1
        steps = backend.parse_steps(chunk[: end + 1].decode("utf-8", errors="replace"))
        if self.turn_open:  # a step never opens a turn
            for step in steps:
                if self.wake_counting:
                    if step.get("type") == "USER_INPUT":
                        self.wake_counting = False  # a prompt joined: the replies count on
                    elif isinstance(step.get("output_tokens"), int):
                        self.wake_tokens += step["output_tokens"]
                for event in backend.step_events(step):
                    apply(self.turn, event)
        return steps


class Agy(AgentPlugin):
    id = "agy"
    topics = [AgyTopic]
    DEFAULT_MODELS = DEFAULT_MODELS
    DEFAULT_PERMISSION_MODE = DEFAULT_PERMISSION_MODE

    def __init__(self, roots=None, model=None, models=None, brief=None, permission_mode=None):
        super().__init__(roots, model, models, brief, permission_mode)
        self.agy_bin = agy_bin()
        self.accounts_root = accounts_root()
        self.offered: list[list[str]] = []  # the models agy lists for a session
        self.model_names: dict[str, str] = {}  # model option value → its display name
        # account data dir → its `/usage` quota groups; the newest poll's, kept on failure
        self.usage_groups: dict[str, list[dict]] = {}
        self.quota_polled: dict[str, float] = {}  # account → when its last poll started
        self.quota_polls: dict[str, asyncio.Task] = {}  # account → its newest poll
        self.quota_noticed: set[str] = set()  # accounts whose failing poll raised a notice

    def model_pool(self) -> list[list[str]]:
        return self.models or self.offered

    def offer_models(self, session: dict) -> str | None:
        """Keep the `model` config option's choices a session reply carries; returns
        its current value."""
        for option in session.get("configOptions") or []:
            if option.get("id") == "model":
                self.offered = [
                    [o.get("name") or o["value"], o["value"]] for o in option.get("options") or []
                ]
                self.model_names.update({value: name for name, value in self.offered})
                return option.get("currentValue")
        return None

    # ── Quota ──────────────────────────────────────────────────────
    def poll_quota(self, topic: AgyTopic) -> None:
        """After a turn: start a `/usage` poll of the window's account, unless one ran
        for it within QUOTA_POLL_INTERVAL or one is still running."""
        key = topic._account_key()
        now = time.monotonic()
        running = self.quota_polls.get(key)
        if running is not None and not running.done():
            return
        if key in self.quota_polled and now - self.quota_polled[key] < QUOTA_POLL_INTERVAL:
            return
        self.quota_polled[key] = now
        self.quota_polls[key] = asyncio.get_running_loop().create_task(self._poll_quota(topic, key))

    async def _poll_quota(self, topic: AgyTopic, key: str) -> None:
        """Run `agy --output-format json -p=/usage` (as the account's `HOME` for a
        non-default one) and keep its groups; a failed poll keeps the last ones and
        raises one notice (in General, not as an agent prompt) per account until a poll
        succeeds again."""
        env = {**os.environ, "PATH": f"{Path.home()}/.local/bin:" + os.environ.get("PATH", "")}
        if topic.config_dir:
            env["HOME"] = topic.config_dir
        try:
            proc = await topic.spawn(
                self.agy_bin,
                "--output-format",
                "json",
                "-p=/usage",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                cwd=topic.workspace,
                env=env,
            )
            try:
                out, _ = await asyncio.wait_for(proc.communicate(), QUOTA_POLL_TIMEOUT)
            except TimeoutError:
                await reap(proc)
                raise RuntimeError(
                    f"`agy -p=/usage` gave no answer in {QUOTA_POLL_TIMEOUT:.0f}s"
                ) from None
            if proc.returncode != 0:
                raise RuntimeError(f"`agy -p=/usage` exited {proc.returncode}")
            groups = backend.usage_groups(json.loads(out))
            if not groups:
                raise ValueError("`agy -p=/usage` listed no quota groups")
        except (OSError, RuntimeError, ValueError) as error:  # ValueError: not JSON
            LOGGER.warning("agy quota poll failed (%s): %r", key, error)
            if key not in self.quota_noticed:
                self.quota_noticed.add(key)
                notice_exception(self.notice, None, f"agy quota poll ({key})", error)
            return
        self.quota_noticed.discard(key)
        self.usage_groups[key] = groups

    # ── Accounts ───────────────────────────────────────────────────
    @property
    def default_account_dir(self) -> Path:
        return self.accounts_root

    def account_name(self, config_dir: Path) -> str:
        if config_dir.name.startswith(ACCOUNT_PREFIX):
            return config_dir.name[len(ACCOUNT_PREFIX) :] or "default"
        return "default"

    def account_dir_for(self, name: str) -> Path:
        return self.accounts_root / f"{ACCOUNT_PREFIX}{name}"

    def scan_accounts(self) -> dict[str, str]:
        found = {}
        try:
            for home in sorted(self.accounts_root.glob(f"{ACCOUNT_PREFIX}*")):
                if (data_dir(home) / TOKEN_FILE).exists():
                    found.setdefault(self.account_name(home), str(home))
        except OSError:
            pass
        return found

    def account_status(self) -> list[dict]:
        """Per-account sign-in state: whether its data dir holds a token file (the
        file is never read)."""
        accounts = {
            name: d
            for name, d in {**self.accounts, **self.scan_accounts()}.items()
            if Path(d) != self.accounts_root
        }
        accounts["default"] = str(self.accounts_root)
        rows = []
        for name, home in sorted(accounts.items()):
            signed_in = (data_dir(Path(home)) / TOKEN_FILE).exists()
            label = f"✅ {name} — signed in" if signed_in else f"❌ {name} — not signed in"
            rows.append({"name": name, "dir": home, "label": label})
        return rows

    async def on_startup(self):
        setup.install_agy_as(self.notice)
        self._load()
        self.seed_accounts()

    async def _run_login(self, io, name: str, cfg: Path) -> bool:
        """`agy acp --login` into `cfg`'s data dir, in a PTY with the displays unset
        (it prints the sign-in URL and reads the code only on a terminal); the URL goes
        to the chat, the code is typed back within agy's sign-in window."""
        cfg.mkdir(parents=True, exist_ok=True)
        await io.send(f"starting login for '{name}'…")
        env = {**os.environ, "PATH": f"{Path.home()}/.local/bin:" + os.environ.get("PATH", "")}
        env["BROWSER"] = "false"
        env.pop("DISPLAY", None)
        env.pop("WAYLAND_DISPLAY", None)
        cmd = [self.agy_bin, "acp", "--login"]
        if cfg != self.accounts_root:
            cmd[2:2] = ["--gemini-dir", str(gemini_dir(cfg))]
        master, slave = pty.openpty()
        try:
            proc = await io.spawn(  # reaped with the window if it closes mid-login
                *cmd, stdin=slave, stdout=slave, stderr=slave, env=env, start_new_session=True
            )
        finally:
            os.close(slave)
        loop = asyncio.get_running_loop()
        buf = ""
        try:
            while "http" not in buf:
                chunk = await asyncio.wait_for(
                    loop.run_in_executor(None, os.read, master, 2048), 25
                )
                if not chunk:
                    break
                buf += chunk.decode(errors="replace")
        except (TimeoutError, OSError):
            pass
        token = data_dir(cfg) / TOKEN_FILE
        try:
            match = re.search(r"https?://\S+", buf)
            if match is None:
                await io.send(f"login produced no URL:\n{buf[-800:]}")
                return False
            code = await io.ask_text(
                f"Open this to sign in, then paste the code back here:\n{match.group(0)}",
                timeout=LOGIN_WINDOW,
            )
            if not code:
                await io.send("login timed out — cancelled")
                return False
            os.write(master, (code.strip() + "\n").encode())
            try:
                await asyncio.wait_for(proc.wait(), LOGIN_WINDOW)
            except TimeoutError:
                pass
            if not token.exists():
                await io.send(f"login didn't complete [exit {proc.returncode}]")
                return False
            return True
        finally:
            await reap(proc)
            os.close(master)
