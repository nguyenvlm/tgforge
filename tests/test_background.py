"""Unit tests for background-task tracking (register, complete, render)."""

from __future__ import annotations

from types import SimpleNamespace

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
