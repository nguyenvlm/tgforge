"""Background-task tracking for the Claude driver: register a launched task from
its tool_result, detect completion/orphaning, and render the live panel.
"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path

from tgforge.base.kernel import file_held_open
from tgforge.base.ui import fmt_duration
from tgforge.plugins.claude.render import SPINNER

LOGGER = logging.getLogger("tgforge")

_TASK_FILE_RE = re.compile(r"(/[^\s'\"]+/tasks/[^\s'\"]+\.output)")
_TASK_DONE_RE = re.compile(
    r"<task-id>\s*([\w-]+)\s*</task-id>.*?<status>\s*(\w+)\s*</status>", re.DOTALL
)
PROBE_INTERVAL = 30.0  # orphan-probe cadence (seconds)
PROBE_GRACE = 90.0  # never probe a task younger than this


def _tail_file(path: str, n: int = 400) -> str:
    try:
        p = Path(path)
        size = p.stat().st_size
        with open(p, "rb") as f:
            if size > n:
                f.seek(size - n)
            data = f.read()
        return data.decode(errors="replace").strip()
    except OSError:
        return ""


def last_line(path: str) -> str:
    lines = [ln for ln in _tail_file(path).splitlines() if ln.strip()]
    return lines[-1][:60] if lines else ""


def panel(session, spin: int = 0) -> tuple[str, str] | None:
    """A monospace, never-collapsed panel of background tasks (md, plain), or None.
    One row per task: a mark (spinner while running, ✓/✗/◼ when done), label,
    elapsed, and the last output line. `spin` animates the mark."""
    tasks = session.background_tasks
    if not tasks:
        return None
    frame = SPINNER[spin % len(SPINNER)]
    n = len(tasks)
    rows = [f"background · {n} job{'s' if n != 1 else ''}"]
    now = time.monotonic()
    for t in tasks.values():
        mark = t["done"] or frame
        el = fmt_duration(int((t.get("done_at") or now) - t["start"]))
        label = t["label"][:24]
        tail = last_line(t["path"]) or ("done" if t["done"] else "running…")
        rows.append(f"{mark} {label:<24} {el:>5}  {tail}")
    body = "\n".join(rows)
    esc = body.replace("\\", "\\\\").replace("`", "\\`")
    return f"```\n{esc}\n```", body


def _output_file_held_open(path: str) -> bool:
    """True if any process holds `path` open — the harness keeps a task's output
    file open for its lifetime, so a closed file means the process tree is gone."""
    return file_held_open(path)


def mark_orphans(session) -> None:
    """Mark running tasks whose output file nothing holds open as ◼ (ended,
    outcome unknown). Throttled; a fresh task gets a grace period first."""
    now = time.monotonic()
    if now - session.background_probe_at < PROBE_INTERVAL:
        return
    session.background_probe_at = now
    for t in session.background_tasks.values():
        if t["done"] is None and now - t["start"] > PROBE_GRACE:
            if not _output_file_held_open(t["path"]):
                t["done"] = "◼"
                t["done_at"] = now
                LOGGER.info("bg task orphaned (no process holds %s)", t["path"])


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
        if any(t["label"] == label for t in session.background_tasks.values()):
            label = f"{label} ({bid[:3]})"
        session.background_tasks[bid] = {
            "path": m.group(1),
            "label": label,
            "start": time.monotonic(),
            "done": None,
        }
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
        bid = fp.rsplit("/", 1)[-1][: -len(".output")]
        if bid in session.background_tasks:
            err = any(
                isinstance(b, dict) and b.get("type") == "tool_result" and b.get("is_error")
                for b in (content if isinstance(content, list) else [])
            )
            session.background_tasks[bid]["done"] = "✗" if err else "✓"
            session.background_tasks[bid]["done_at"] = time.monotonic()
            return bid
    return None
