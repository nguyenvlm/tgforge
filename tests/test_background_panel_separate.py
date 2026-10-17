"""The background-jobs panel's content: running jobs plus each finished job's outcome
for a short linger. Its placement (its own bottom-most message) is in test_panel_bottom.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

from tgforge.plugins.claude import background


def _session(tmp_path, finished_ago):
    out = tmp_path / "bid.output"
    out.write_text("still going\n")
    now = time.monotonic()
    return SimpleNamespace(
        background_tasks={
            "run": {"path": str(out), "label": "live-job", "start": now - 60, "done": None},
            "fin": {
                "path": str(out),
                "label": "old-job",
                "start": now - 120,
                "done": "✗",
                "done_at": now - finished_ago,
            },
        },
        background_labels={},
        background_probe_at=0.0,
    )


def test_finished_job_shows_its_outcome_while_it_lingers(tmp_path):
    _md, plain = background.panel(_session(tmp_path, finished_ago=1.0), 0)
    assert "live-job" in plain
    old_row = next(row for row in plain.splitlines() if "old-job" in row)
    assert old_row.startswith("✗") and old_row.endswith("failed")
    assert "background · 1 job · 1 finished" in plain


def test_finished_job_drops_off_after_the_linger(tmp_path):
    s = _session(tmp_path, finished_ago=background.FINISHED_LINGER + 1)
    _md, plain = background.panel(s, 0)
    assert "old-job" not in plain and "✗" not in plain
    assert "background · 1 job" in plain and "finished" not in plain
    background.prune_finished(s, time.monotonic())
    assert list(s.background_tasks) == ["run"]


def test_many_jobs_trim_the_oldest_rows_to_fit_one_message(tmp_path):
    from tgforge.base.ui import MAX_MSG

    out = tmp_path / "bid.output"
    out.write_text("x" * 50 + "\n")
    now = time.monotonic()
    session = SimpleNamespace(
        background_tasks={
            f"b{i}": {"path": str(out), "label": f"job {i:03d}", "start": now, "done": None}
            for i in range(200)
        },
        background_labels={},
        background_probe_at=0.0,
    )
    md, plain = background.panel(session, 0)
    assert len(md) <= MAX_MSG
    assert "older job(s) hidden" in plain
    assert "job 199" in plain and "job 000" not in plain  # the oldest go first
