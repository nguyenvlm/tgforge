"""Unit tests for background-task tracking (register, complete, render)."""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from tgforge.plugins.claude import background


def _session():
    return SimpleNamespace(background_tasks={}, background_labels={}, background_probe_at=0.0)


def test_launch_labels_prefers_description():
    ev = {
        "type": "assistant",
        "message": {
            "content": [
                {
                    "type": "tool_use",
                    "id": "t1",
                    "name": "Bash",
                    "input": {
                        "command": "make test",
                        "description": "run tests",
                        "run_in_background": True,
                    },
                },
                {
                    "type": "tool_use",
                    "id": "t2",
                    "name": "Bash",
                    "input": {"command": "ls"},
                },  # not bg
            ]
        },
    }
    assert background.launch_labels(ev) == {"t1": "run tests"}


def test_scan_register_then_done():
    s = _session()
    s.background_labels["t1"] = "run tests"
    launch = {
        "type": "user",
        "tool_use_result": {"backgroundTaskId": "bid9"},
        "message": {
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "t1",
                    "content": "Output is being written to: /srv/data/tasks/bid9.output",
                }
            ]
        },
    }
    background.register_task(s, launch)
    assert "bid9" in s.background_tasks
    assert s.background_tasks["bid9"]["label"] == "run tests"
    assert s.background_tasks["bid9"]["done"] is None

    done = {
        "type": "user",
        "message": {"content": "<task-id>bid9</task-id> <status>completed</status>"},
    }
    assert background.mark_done(s, done) == "bid9"
    assert s.background_tasks["bid9"]["done"] == "✓"


def test_bg_panel_none_when_empty():
    assert background.panel(_session()) is None


def test_bg_panel_renders_rows(tmp_path):
    out = tmp_path / "bid.output"
    out.write_text("line one\nfinal line\n")
    s = _session()
    s.background_tasks["bid"] = {"path": str(out), "label": "job", "start": 0.0, "done": None}
    md, plain = background.panel(s, 0)
    assert "background · 1 job" in plain
    assert "final line" in plain
    assert md.startswith("```")


def _aged_task(path):
    """A running task past the probe grace, due for a probe."""
    s = _session()
    s.background_probe_at = float("-inf")
    s.background_tasks["bid"] = {
        "path": str(path),
        "label": "job",
        "start": time.monotonic() - background.PROBE_GRACE - 1,
        "done": None,
    }
    return s


@pytest.mark.parametrize(
    ("content", "mark"),
    [
        ("out\n\n[exited with code 0]\n", "✓"),
        ("out\n\n[exited with code 3]\n", "✗"),
        ("out\n[exited with code 144]", "✗"),
        ("out\n\n[exited with code unknown]\n", "✗"),
        ("out\n\n[killed]\n", "✗"),
        ("out\nno marker\n", "◼"),
        ("", "◼"),
        ("[exited with code 0]\nlater output\n", "◼"),  # marker text is not the last line
    ],
)
def test_mark_orphans_reads_exit_marker_of_closed_file(tmp_path, content, mark):
    out = tmp_path / "bid.output"
    out.write_text(content)
    s = _aged_task(out)
    background.mark_orphans(s)
    assert s.background_tasks["bid"]["done"] == mark


def test_mark_orphans_leaves_held_open_file_without_marker_running(tmp_path):
    out = tmp_path / "bid.output"
    out.write_text("still working\n")
    s = _aged_task(out)
    with open(out):  # this process holds it open, as a live job's shell would
        background.mark_orphans(s)
    assert s.background_tasks["bid"]["done"] is None


def _young_task(path):
    """A running task inside its probe grace."""
    s = _aged_task(path)
    s.background_tasks["bid"]["start"] = time.monotonic()
    return s


@pytest.mark.parametrize(("code", "mark"), [("0", "✓"), ("2", "✗")])
def test_mark_orphans_reads_marker_inside_grace(tmp_path, code, mark):
    out = tmp_path / "bid.output"
    out.write_text(f"out\n\n[exited with code {code}]\n")
    s = _young_task(out)
    background.mark_orphans(s)
    assert s.background_tasks["bid"]["done"] == mark


def test_mark_orphans_marker_wins_over_held_open(tmp_path):
    out = tmp_path / "bid.output"
    out.write_text("out\n\n[exited with code 0]\n")
    s = _aged_task(out)
    with open(out):  # a leftover holder does not undo a written marker
        background.mark_orphans(s)
    assert s.background_tasks["bid"]["done"] == "✓"


def test_mark_orphans_keeps_grace_without_marker(tmp_path):
    out = tmp_path / "bid.output"
    out.write_text("no marker yet\n")  # nothing holds it, but the task is young
    s = _young_task(out)
    background.mark_orphans(s)
    assert s.background_tasks["bid"]["done"] is None


def _read_result(path):
    """The Read tool's result on a job's output file."""
    return {
        "type": "user",
        "tool_use_result": {"type": "text", "file": {"filePath": str(path)}},
        "message": {"content": [{"type": "tool_result", "tool_use_id": "r1", "content": "…"}]},
    }


def test_read_of_a_running_jobs_output_leaves_it_running(tmp_path):
    out = tmp_path / "tasks" / "bid.output"
    out.parent.mkdir()
    out.write_text("epoch 3/10\n")  # interim output, no exit marker yet
    s = _session()
    s.background_tasks["bid"] = {"path": str(out), "label": "train", "start": 0.0, "done": None}
    assert background.mark_done(s, _read_result(out)) is None
    assert s.background_tasks["bid"]["done"] is None


@pytest.mark.parametrize(("code", "mark"), [("0", "✓"), ("1", "✗")])
def test_read_of_a_finished_jobs_output_takes_the_marker(tmp_path, code, mark):
    out = tmp_path / "tasks" / "bid.output"
    out.parent.mkdir()
    out.write_text(f"epoch 10/10\n\n[exited with code {code}]\n")
    s = _session()
    s.background_tasks["bid"] = {"path": str(out), "label": "train", "start": 0.0, "done": None}
    assert background.mark_done(s, _read_result(out)) == "bid"
    assert s.background_tasks["bid"]["done"] == mark
