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
from tgforge.base.ui import MAX_MSG, fmt_duration
from tgforge.plugins.claude.render import SPINNER

LOGGER = logging.getLogger("tgforge")

_TASK_FILE_RE = re.compile(r"(/[^\s'\"]+/tasks/[^\s'\"]+\.output)")
_TASK_DONE_RE = re.compile(
    r"<task-id>\s*([\w-]+)\s*</task-id>.*?<status>\s*(\w+)\s*</status>", re.DOTALL
)
# the line the CLI appends to a background Bash job's output file once it ends
_EXIT_MARKER_RE = re.compile(r"\[(?:exited with code (\S+)|killed)\]")
PROBE_INTERVAL = 30.0  # orphan-probe cadence (seconds)
PROBE_GRACE = 90.0  # never probe a task younger than this
FINISHED_LINGER = 30.0  # seconds a finished task's row stays on the panel
_OUTCOME = {"✓": "done", "✗": "failed", "◼": "ended"}


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


def visible_tasks(session, now: float) -> list[dict]:
    """The panel's tasks, in launch order: every running one, plus each finished one
    for FINISHED_LINGER seconds after it ended."""
    return [
        t
        for t in session.background_tasks.values()
        if t["done"] is None or now - t["done_at"] <= FINISHED_LINGER
    ]


def prune_finished(session, now: float) -> None:
    """Forget finished tasks once their panel row has lingered out."""
    for bid in [
        b
        for b, t in session.background_tasks.items()
        if t["done"] is not None and now - t["done_at"] > FINISHED_LINGER
    ]:
        del session.background_tasks[bid]


def panel(session, spin: int = 0) -> tuple[str, str] | None:
    """A monospace, never-collapsed panel of background tasks (md, plain), or None when
    there are none to show. A running row: the spinner, label, elapsed, and the last
    output line (`spin` animates the spinner). A finished row keeps its outcome mark
    (✓ / ✗ / ◼) for FINISHED_LINGER seconds, then drops. The panel is its own message,
    so if many jobs make it exceed a Telegram message the OLDEST rows are dropped
    first — the panel never costs the main text."""
    now = time.monotonic()
    shown = visible_tasks(session, now)
    if not shown:
        return None
    frame = SPINNER[spin % len(SPINNER)]
    running = sum(1 for t in shown if t["done"] is None)
    counts = []
    if running:
        counts.append(f"{running} job{'s' if running != 1 else ''}")
    if running < len(shown):
        counts.append(f"{len(shown) - running} finished")
    header = "background · " + " · ".join(counts)
    task_rows = []
    for t in shown:
        label = t["label"][:24]
        if t["done"] is None:
            el = fmt_duration(int(now - t["start"]))
            tail = "✖ stopping" if t.get("stopping") else (last_line(t["path"]) or "running…")
            task_rows.append(f"{frame} {label:<24} {el:>5}  {tail}")
        else:
            el = fmt_duration(int(t["done_at"] - t["start"]))
            task_rows.append(f"{t['done']} {label:<24} {el:>5}  {_OUTCOME[t['done']]}")
    body = _fit_panel(header, task_rows)
    esc = body.replace("\\", "\\\\").replace("`", "\\`")
    return f"```\n{esc}\n```", body


def _fit_panel(header: str, task_rows: list[str]) -> str:
    """Join header + rows; if the escaped code block would exceed a Telegram message,
    drop the oldest rows (front of the list) until it fits, marking the elision."""
    limit = MAX_MSG - 10  # leave room for the ``` fences and a backtick-escape or two
    rows = list(task_rows)
    while rows:
        shown = [header, *rows]
        if len(rows) < len(task_rows):
            shown.insert(1, f"… {len(task_rows) - len(rows)} older job(s) hidden")
        body = "\n".join(shown)
        if len(body) <= limit:
            return body
        rows.pop(0)
    return header


def _output_file_held_open(path: str) -> bool:
    """True if any process holds `path` open — the harness keeps a task's output
    file open for its lifetime, so a closed file means the process tree is gone."""
    return file_held_open(path)


def exit_mark(path: str) -> str | None:
    """✓ / ✗ from the exit marker ending a finished Bash job's output file (`[exited
    with code N]`, `[killed]`), or None when the last line is not a marker."""
    lines = [ln for ln in _tail_file(path).splitlines() if ln.strip()]
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
    for t in session.background_tasks.values():
        if t["done"] is not None:
            continue
        mark = exit_mark(t["path"])
        if mark is None:
            if now - t["start"] <= PROBE_GRACE or _output_file_held_open(t["path"]):
                continue
            mark = "◼"
        t["done"] = mark
        t["done_at"] = now
        LOGGER.info("bg task ended %s (%s)", mark, t["path"])


def match_jobs(running: dict[str, dict], name: str) -> list[str]:
    """Ids of the running jobs `name` picks: an exact label (case-insensitive) wins,
    else every job whose label or id starts with it."""
    key = name.lower()
    exact = [bid for bid, t in running.items() if t["label"].lower() == key]
    if exact:
        return exact
    return [
        bid
        for bid, t in running.items()
        if t["label"].lower().startswith(key) or bid.lower().startswith(key)
    ]


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
        # a Read of the job's output file: also used to check interim output, so the
        # job is done only once the file ends with its exit marker
        bid = fp.rsplit("/", 1)[-1][: -len(".output")]
        task = session.background_tasks.get(bid)
        mark = exit_mark(task["path"]) if task else None
        if mark is not None and task["done"] is None:
            task["done"] = mark
            task["done_at"] = time.monotonic()
            return bid
    return None
