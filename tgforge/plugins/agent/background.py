"""Background jobs of an agent window, backend-neutral: the job table each backend
fills (`add_job`, `end_job`), its pruning, and the live panel. A job is a dict:
`label`, `start`, `done` (None while running, else its ✓ / ✗ / ◼ mark), `done_at`,
`path` (its output file, when it has one), `stopping`.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from tgforge.base.ui import MAX_MSG, code_block, fmt_duration, most_that_fits
from tgforge.plugins.agent.render import SPINNER

LOGGER = logging.getLogger("tgforge")

FINISHED_LINGER = 30.0  # seconds a finished task's row stays on the panel
_OUTCOME = {"✓": "done", "✗": "failed", "◼": "ended"}


def add_job(session, job_id: str, label: str, path: str | None = None) -> None:
    """Register a running job; a label another job already has gets the id's head."""
    if any(t["label"] == label for t in session.background_tasks.values()):
        label = f"{label} ({job_id[:3]})"
    session.background_tasks[job_id] = {
        "path": path,
        "label": label,
        "start": time.monotonic(),
        "done": None,
    }


def end_job(session, job_id: str, mark: str) -> bool:
    """Mark a running job ended with `mark`; False when it is unknown or already ended."""
    task = session.background_tasks.get(job_id)
    if task is None or task["done"] is not None:
        return False
    task["done"] = mark
    task["done_at"] = time.monotonic()
    LOGGER.info("bg task ended %s (%s)", mark, task["label"])
    return True


def tail_file(path: str, n: int = 400) -> str:
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


def last_line(path: str | None) -> str:
    lines = [ln for ln in tail_file(path).splitlines() if ln.strip()] if path else []
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


def panel(session, spin: int = 0, fits=None) -> tuple[str, str] | None:
    """A monospace, never-collapsed panel of background tasks (Markdown, plain), or None when
    there are none to show. A running row: the spinner, label, elapsed, and the last
    output line (`spin` animates the spinner). A finished row keeps its outcome mark
    (✓ / ✗ / ◼) for FINISHED_LINGER seconds, then drops. The panel is its own message,
    so if many jobs make it exceed a message the OLDEST rows are dropped
    first — the panel never costs the main text; given `fits` (`Topic.fits_one_message`),
    until its code block fits one message in every form."""
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
    body = _fit_panel(header, task_rows, fits)
    return code_block(body), body


def _fit_panel(header: str, task_rows: list[str], fits=None) -> str:
    """Join header + rows; if the code block would exceed a message (or fail `fits`),
    drop the oldest rows (front of the list) until it fits, marking the elision."""
    limit = MAX_MSG - 10  # leave room for the ``` fences and a backtick-escape or two

    def body(kept: int) -> str:
        shown = [header, *task_rows[len(task_rows) - kept :]]
        if kept < len(task_rows):
            shown.insert(1, f"… {len(task_rows) - kept} older job(s) hidden")
        return "\n".join(shown)

    def block(kept: int) -> str:
        return code_block(body(kept))

    def fitting(md: str) -> bool:
        return len(md) <= limit + 8 and (fits is None or fits(md))  # 8: the fences

    return body(most_that_fits(block, len(task_rows), fitting))


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
