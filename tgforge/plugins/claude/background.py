"""Claude's background jobs: register a launched task from its tool_result and detect
its end (a completion event, the exit marker in its output file, an orphaned file),
into the agent core's job table (`tgforge.plugins.agent.background`).
"""

from __future__ import annotations

import json
import logging
import re
import time

from tgforge.base.kernel import file_held_open
from tgforge.plugins.agent.background import add_job, end_job, tail_file

LOGGER = logging.getLogger("tgforge")

_TASK_FILE_RE = re.compile(r"(/[^\s'\"]+/tasks/[^\s'\"]+\.output)")
_TASK_DONE_RE = re.compile(
    r"<task-id>\s*([\w-]+)\s*</task-id>.*?<status>\s*(\w+)\s*</status>", re.DOTALL
)
# the line the CLI appends to a background Bash job's output file once it ends
_EXIT_MARKER_RE = re.compile(r"\[(?:exited with code (\S+)|killed)\]")
PROBE_INTERVAL = 30.0  # orphan-probe cadence (seconds)
PROBE_GRACE = 90.0  # never probe a task younger than this


def _output_file_held_open(path: str) -> bool:
    """True if any process holds `path` open — the harness keeps a task's output
    file open for its lifetime, so a closed file means the process tree is gone."""
    return file_held_open(path)


def exit_mark(path: str) -> str | None:
    """✓ / ✗ from the exit marker ending a finished Bash job's output file (`[exited
    with code N]`, `[killed]`), or None when the last line is not a marker."""
    lines = [ln for ln in tail_file(path).splitlines() if ln.strip()]
    m = _EXIT_MARKER_RE.fullmatch(lines[-1].strip()) if lines else None
    if m is None:
        return None
    return "✓" if m.group(1) == "0" else "✗"


def mark_orphans(session) -> None:
    """Mark ended running tasks. A written exit marker is definitive, so a task ends at
    the first probe that sees one (✓ / ✗). Without one, a task past its grace period
    whose output file nothing holds open ends as ◼ (outcome unknown). Throttled."""
    now = time.monotonic()
    if now - session.background_probe_at < PROBE_INTERVAL:
        return
    session.background_probe_at = now
    for bid, t in list(session.background_tasks.items()):
        if t["done"] is not None:
            continue
        mark = exit_mark(t["path"])
        if mark is None:
            if now - t["start"] <= PROBE_GRACE or _output_file_held_open(t["path"]):
                continue
            mark = "◼"
        end_job(session, bid, mark)


def launch_labels(ev: dict) -> dict[str, str]:
    """Map tool_use_id -> human label for each background launch in an event."""
    out: dict[str, str] = {}
    if ev.get("type") != "assistant":
        return out
    for b in ev.get("message", {}).get("content") or []:
        if not (isinstance(b, dict) and b.get("type") == "tool_use"):
            continue
        inp = b.get("input") if isinstance(b.get("input"), dict) else {}
        is_bg_bash = b.get("name") == "Bash" and inp.get("run_in_background")
        is_agent = b.get("name") == "Agent" and inp.get("run_in_background", True)
        if not (is_bg_bash or is_agent):
            continue
        label = (inp.get("description") or "").strip()
        if not label:
            cmd = (inp.get("command") or "").strip()
            label = cmd.splitlines()[0] if cmd else ("subagent" if is_agent else "task")
        out[b.get("id")] = label
    return out


def register_task(session, ev: dict) -> None:
    """Register a background task from its launch tool_result (has backgroundTaskId
    + a /tasks/*.output path)."""
    tur = ev.get("tool_use_result")
    if not (isinstance(tur, dict) and tur.get("backgroundTaskId")):
        return
    bid = tur["backgroundTaskId"]
    if bid in session.background_tasks:
        return
    for b in ev.get("message", {}).get("content") or []:
        if not (isinstance(b, dict) and b.get("type") == "tool_result"):
            continue
        c = b.get("content")
        text = c if isinstance(c, str) else json.dumps(c)
        m = _TASK_FILE_RE.search(text)
        if not m:
            continue
        label = session.background_labels.get(b.get("tool_use_id")) or bid
        add_job(session, bid, label, m.group(1))
        return


def mark_done(session, ev: dict) -> str | None:
    """Mark a tracked task done from its completion event; returns the id or None."""
    content = ev.get("message", {}).get("content")
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        text = " ".join(
            b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"
        )
    else:
        text = ""
    for bid, status in _TASK_DONE_RE.findall(text):
        if bid in session.background_tasks:
            session.background_tasks[bid]["done"] = "✓" if status == "completed" else "✗"
            session.background_tasks[bid]["done_at"] = time.monotonic()
            return bid
    tur = ev.get("tool_use_result")
    f = tur.get("file") if isinstance(tur, dict) else None
    fp = f.get("filePath") if isinstance(f, dict) else None
    if fp and _TASK_FILE_RE.search(fp):
        # a Read of the job's output file: also used to check interim output, so the
        # job is done only once the file ends with its exit marker
        bid = fp.rsplit("/", 1)[-1][: -len(".output")]
        task = session.background_tasks.get(bid)
        mark = exit_mark(task["path"]) if task else None
        if mark is not None and end_job(session, bid, mark):
            return bid
    return None
