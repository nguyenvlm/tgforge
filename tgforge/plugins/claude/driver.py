"""ClaudeTopic + Claude plugin: the Claude CLI backend of the agent window.

Each `/claude` window is one `ClaudeTopic`, an `AgentTopic` (`tgforge.plugins.agent`)
whose backend spawns and drives the CLI (stream-json in/out): its reader turns the
stream into the core's neutral turn events (`events.turn_events`) and calls the core
at each prompt replay and `result`. The mirror reads the session's jsonl transcript;
background jobs are found from launch results and their output files (`background`).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import time
import uuid
from pathlib import Path

from tgforge.base.kernel import kill_file_holders, launch, notice_exception, reap
from tgforge.plugins.agent.window import (
    TURN_IDLE,
    TURN_KEPT,
    AgentPlugin,
    AgentTopic,
    system_prompt_file,
)
from tgforge.plugins.claude import background, setup
from tgforge.plugins.claude.config import DEFAULT_MODELS, claude_bin, claude_dir
from tgforge.plugins.claude.events import (
    apply_event,
    has_background_launch,
    is_notification_result,
    is_prompt_replay,
    mirror_parse,
    prompt_block_count,
    rate_limit_windows,
    result_context_window,
)

LOGGER = logging.getLogger("tgforge")

CONTROL_TIMEOUT = 10.0  # seconds to wait for the CLI's answer to a control request
PROJECT_DIR_MAX = 200  # the CLI's longest project dir name kept whole
READER_TEARDOWN_WAIT = 5.0  # seconds an ended process's reader gets to finish its teardown

# Claude CLI --permission-mode values, passed on every spawn. The app default is
# `auto` (a bot has no human to answer prompts); set it on the plugin or via /mode.
PERMISSION_MODES = ("auto", "acceptEdits", "bypassPermissions", "dontAsk", "manual", "plan")
DEFAULT_PERMISSION_MODE = "auto"


def _project_dir(config_dir: Path, workspace: Path) -> Path:
    """The CLI's transcript dir for a cwd, named as the CLI names it: every UTF-16 unit
    that is not an ASCII letter or digit becomes `-`; a name over `PROJECT_DIR_MAX`
    is cut to that length plus `-` and the base-36 Java-style string hash of the path."""
    raw = str(workspace).encode("utf-16-le", "surrogatepass")
    units = [int.from_bytes(raw[i : i + 2], "little") for i in range(0, len(raw), 2)]
    name = "".join(chr(u) if chr(u).isascii() and chr(u).isalnum() else "-" for u in units)
    if len(name) > PROJECT_DIR_MAX:
        digest = 0
        for u in units:
            digest = (digest * 31 + u) & 0xFFFFFFFF
        digest = abs(digest - (1 << 32) if digest >= 1 << 31 else digest)
        suffix = ""
        while True:
            digest, digit = divmod(digest, 36)
            suffix = "0123456789abcdefghijklmnopqrstuvwxyz"[digit] + suffix
            if not digest:
                break
        name = f"{name[:PROJECT_DIR_MAX]}-{suffix}"
    return config_dir / "projects" / name


@launch("/claude", "open a Claude agent window")
class ClaudeTopic(AgentTopic):
    id = "claude"
    icon = "🤖"
    menu_label = "Claude"
    agent_name = "Claude"
    PERMISSION_MODES = PERMISSION_MODES

    def __init__(self, core, window, name, saved):
        super().__init__(core, window, name, saved)
        self.proc: asyncio.subprocess.Process | None = None
        self.reader_task: asyncio.Task | None = None
        self.ended_proc = None  # the process the window ended: gone for it at once
        self.control_waiters: dict[str, asyncio.Future] = {}  # request_id → its response
        self.background_probe_at = 0.0

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

    def _proj_dir(self) -> Path:
        return _project_dir(self._session_config_dir(), Path(self.workspace))

    def _jsonl(self) -> Path:
        return self._proj_dir() / f"{self.session_id}.jsonl"

    def _transcript_path(self) -> Path:
        return self._jsonl()

    def _mirror_parse(self, raw: str):
        return mirror_parse(raw)

    def _probe_jobs(self) -> None:
        background.mark_orphans(self)

    def _cli_line(self) -> str:
        cfg = str(self._session_config_dir())
        return f"cd {self.workspace} && CLAUDE_CONFIG_DIR={cfg} claude --resume {self.session_id}"

    # ── Backend boundary ───────────────────────────────────────────
    def backend_alive(self) -> bool:
        return (
            self.proc is not None
            and self.proc is not self.ended_proc
            and self.proc.returncode is None
        )

    async def backend_start(self) -> None:
        await self._ensure_proc()

    async def backend_prompt(self, text: str) -> None:
        await self._write_prompt(text)

    async def backend_cancel(self) -> bool:
        request_id = await self._control("interrupt")
        if request_id is None:
            return False
        self.control_waiters.pop(request_id, None)  # nobody awaits the answer
        return True

    async def backend_set_option(self, name: str, value: str | None) -> None:
        """The CLI takes the model, effort and mode at spawn: an idle process is dropped
        so the next message respawns with them."""
        self._respawn_idle()

    def backend_end(self) -> None:
        self.ended_proc = self.proc  # gone for the window at once, before its exit is seen
        self.proc.kill()

    async def backend_close(self) -> None:
        if self.reader_task is not None:
            self.reader_task.cancel()
        if self.proc:
            await reap(self.proc)

    async def backend_stop_job(self, bid: str, task: dict) -> None:
        """Stop one background job through the CLI that owns it (`stop_task` kills the
        job's whole process tree); with no live CLI, kill the processes holding its
        output file instead."""
        label = task["label"]
        request_id = await self._control("stop_task", task_id=bid)
        if request_id is not None:
            waiter = self.control_waiters[request_id]
            task["stopping"] = True
            await self._send_turn_note(f"stopping {label}…")
            try:
                response = await asyncio.wait_for(waiter, CONTROL_TIMEOUT)
            except TimeoutError:
                response = {"subtype": "error", "error": "no answer from Claude"}
            finally:
                self.control_waiters.pop(request_id, None)
            if response.get("subtype") != "success":
                task["stopping"] = False
                await self.send(
                    f"couldn't stop {label}: {response.get('error') or 'unknown error'}"
                )
            return
        try:
            signalled = await kill_file_holders(task["path"])
        except RuntimeError as exc:
            LOGGER.error("kill (%s): refused for %s: %s", self.name, bid, exc)
            await self.send(f"refused to stop {label}: {exc}")
            return
        if signalled is None:
            await self.send(f"can't stop {label} — no live Claude session to ask")
        elif signalled == 0:
            await self.send(f"{label} has no running processes left")
        else:
            task["stopping"] = True
            await self.send(f"stopping {label}… ({signalled} process(es))")

    # ── The CLI process ────────────────────────────────────────────
    async def _ensure_proc(self):
        if self.backend_alive():
            # reused across turns (kept alive for background tasks); _open_holder arms
            LOGGER.info("proc reuse (%s)", self.name)
            return
        await self._end_reader()
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
        model = self._model_id()
        if model:
            cmd += ["--model", model]
        if self.effort:
            cmd += ["--effort", self.effort]
        cmd += ["--session-id", self.session_id] if is_new else ["--resume", self.session_id]
        cmd += ["--permission-mode", self.plugin.permission_mode]
        prompt_file = system_prompt_file(Path(self.workspace))
        if prompt_file is not None:
            cmd += ["--system-prompt-file", str(prompt_file)]
        cmd += ["--append-system-prompt-file", str(self._write_session_note())]
        env = self._login_env(self._session_config_dir())
        env["TGFORGE_WINDOW"] = self.window
        env["TGFORGE_PLUGIN"] = self.id  # the window store's namespace: `win:<id>:<key>`
        env["TGFORGE_BOT_DRIVEN"] = "1"
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
        self._arm_heartbeat()
        LOGGER.info("proc spawn (%s): reader + heartbeat armed", self.name)

    async def _end_reader(self) -> None:
        """The old process's reader tears down before a new spawn: its teardown clears
        the window's turn state and its process, so it never runs after the spawn."""
        reader = self.reader_task
        if reader is None or reader is asyncio.current_task() or reader.done():
            return
        if self.proc is not None and self.proc.returncode is None:
            await self.proc.wait()  # an ended process: its exit comes at once
        await asyncio.wait({reader}, timeout=READER_TEARDOWN_WAIT)  # its teardown whole
        if not reader.done():  # a teardown stuck behind the lock this spawn holds
            reader.cancel()
            await asyncio.wait({reader})

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
                self._note_activity()
                t = ev.get("type")
                if t == "control_response":
                    self._resolve_control(ev)
                    continue
                if not self.turn_open and t in ("assistant", "stream_event"):
                    self._open_next_turn()
                if t == "rate_limit_event":
                    self._note_quota(ev)
                if t == "assistant":
                    self.background_labels.update(background.launch_labels(ev))
                if t == "user":
                    background.register_task(self, ev)
                    background.mark_done(self, ev)
                    if self.background_tasks:
                        self._ensure_background_updater()
                apply_event(self.turn, ev)
                if t == "user" and is_prompt_replay(ev):
                    replayed = []
                    n = min(prompt_block_count(ev), len(self.pending_writes))
                    for _ in range(n):
                        if not self.turn_open:
                            self._open_next_turn()
                            continue
                        replayed.append(self.pending_writes.pop(0))
                    self._fold_prompts(replayed)
                    self.turn.awaits_replay = False
                elif t == "result":
                    if is_notification_result(ev) and self.turn_open and self.turn.awaits_replay:
                        # the CLI ran a background-task notification ahead of the prompt
                        # that opened this turn; that prompt's replay and result follow
                        LOGGER.info(
                            "reader (%s): notification result before the turn's prompt "
                            "→ turn stays open",
                            self.name,
                        )
                        continue
                    outcome = await self._end_turn(
                        ev.get("result", ""),
                        subtype=ev.get("subtype", "success"),
                        is_error=bool(ev.get("is_error")),
                        context_window=result_context_window(ev, self._model_id()),
                        keep=has_background_tasks,
                    )
                    if outcome == TURN_KEPT:
                        has_background_tasks = False
                    elif outcome == TURN_IDLE:
                        try:
                            self.proc.stdin.close()
                        except (BrokenPipeError, ConnectionResetError, OSError):
                            pass
                        self.backend_end()
                        break
                if has_background_launch(ev):
                    has_background_tasks = True
        except Exception as error:
            LOGGER.exception("reader error (%s)", self.name)
            notice_exception(self._core.notice, self.window, "CLI reader", error)
        finally:
            self._fail_control_waiters()
            await self._process_exited()
            self.proc = None
            self.reader_task = None
            self._save()

    # ── Usage: quota ───────────────────────────────────────────────
    def _note_quota(self, ev: dict) -> None:
        """Keep the newest rate-limit windows per account, shared by its windows."""
        quota = self.plugin.quota.setdefault(self._account_key(), {})
        quota.update(rate_limit_windows(ev))

    # ── Title ──────────────────────────────────────────────────────
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

    # ── Accounts ───────────────────────────────────────────────────
    def _carry_transcript(self, old_dir: Path, new_dir: Path):
        src = _project_dir(old_dir, Path(self.workspace)) / f"{self.session_id}.jsonl"
        dst = _project_dir(new_dir, Path(self.workspace)) / f"{self.session_id}.jsonl"
        if not src.exists():
            return
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)

    # ── Control requests ───────────────────────────────────────────
    async def _control(self, subtype: str, **fields) -> str | None:
        """Write a control request to the live CLI; returns its request_id, or None when
        no live CLI took it. Its response lands in `control_waiters[request_id]`, which
        the caller pops."""
        if not (self.proc and self.proc.returncode is None):
            return None
        request_id = uuid.uuid4().hex[:8]
        ctrl = {
            "type": "control_request",
            "request_id": request_id,
            "request": {"subtype": subtype, **fields},
        }
        # registered before the write: the reader may resolve it while drain() yields
        self.control_waiters[request_id] = asyncio.get_running_loop().create_future()
        try:
            self.proc.stdin.write((json.dumps(ctrl) + "\n").encode())
            await self.proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError, RuntimeError):
            self.control_waiters.pop(request_id, None)
            return None
        return request_id

    def _resolve_control(self, ev: dict) -> None:
        response = ev.get("response") or {}
        waiter = self.control_waiters.get(response.get("request_id"))
        if waiter is not None and not waiter.done():
            waiter.set_result(response)

    def _fail_control_waiters(self) -> None:
        """The CLI is gone: every unanswered control request gets an error response."""
        waiters, self.control_waiters = self.control_waiters, {}
        for waiter in waiters.values():
            if not waiter.done():
                waiter.set_result({"subtype": "error", "error": "the Claude process ended"})


class Claude(AgentPlugin):
    id = "claude"
    topics = [ClaudeTopic]
    DEFAULT_MODELS = DEFAULT_MODELS
    DEFAULT_PERMISSION_MODE = DEFAULT_PERMISSION_MODE

    def __init__(self, roots=None, model=None, models=None, brief=None, permission_mode=None):
        super().__init__(roots, model, models, brief, permission_mode)
        self.claude_dir = claude_dir()
        self.claude_bin = claude_bin()

    @property
    def default_account_dir(self) -> Path:
        return self.claude_dir

    def account_name(self, config_dir: Path) -> str:
        base = config_dir.name.lstrip(".")
        if base.startswith("claude-"):
            base = base[len("claude-") :]
        return base or "default"

    def account_dir_for(self, name: str) -> Path:
        return self.claude_dir.parent / f".claude-{name}"

    async def on_startup(self):
        setup.install_claude_as(self.notice)
        setup.install_shell_wrapper(self.notice)
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

    async def _run_login(self, io, name: str, cfg: Path) -> bool:
        """Run `claude auth login --claudeai` into `cfg` (new or existing). `io` is a
        topic or ctx — anything with send/ask_text/spawn. True on a credentialed result."""
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
        proc = await io.spawn(  # reaped with the window if it closes mid-login
            self.claude_bin,
            "auth",
            "login",
            "--claudeai",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
            start_new_session=True,  # a reap kills whatever the login started too
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
            if not creds.exists():
                label = f"❌ {name} — no credentials"
            else:
                try:
                    oauth = json.loads(creds.read_text()).get("claudeAiOauth", {})
                except (OSError, ValueError):
                    oauth = None
                if oauth is None:
                    label = f"⚠️ {name} — error reading creds"
                else:
                    sub = oauth.get("subscriptionType", "?")
                    exp = oauth.get("expiresAt", 0)
                    rexp = oauth.get("refreshTokenExpiresAt", 0)
                    if rexp and rexp < now_ms:
                        label = f"❌ {name} — {sub} — refresh token expired"
                    elif exp and exp < now_ms:
                        label = f"🔄 {name} — {sub} — access token expired — re-login if sync fails"
                    else:
                        days = (rexp - now_ms) / 86_400_000 if rexp else 0
                        label = f"✅ {name} — {sub} — OK ({days:.0f}d)"
            rows.append({"name": name, "dir": cfg_dir, "label": label})
        return rows
