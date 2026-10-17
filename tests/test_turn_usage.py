"""Context and quota on the summary line and the live status line (AGENTS.md →
Finalized-turn format). Event shapes come from a captured CLI stream
(`fixtures/cli_usage_events.jsonl`)."""

from __future__ import annotations

import asyncio
import copy

from claude_fakes import FakeProc, client, drain, fixture_events, line, result, texts, window

from tgforge.plugins.claude.events import rate_limit_windows, result_context_window
from tgforge.plugins.claude.render import quota_part
from tgforge.plugins.claude.session import TurnState, apply_event


def _message_start(input_tokens, created, read, parent=None):
    ev = copy.deepcopy(fixture_events()["message_start"])
    ev["event"]["message"]["usage"].update(
        input_tokens=input_tokens,
        cache_creation_input_tokens=created,
        cache_read_input_tokens=read,
    )
    ev["parent_tool_use_id"] = parent
    return ev


def test_context_tokens_come_from_the_last_message_start():
    st = TurnState()
    apply_event(st, fixture_events()["message_start"])
    assert st.context_tokens == 10 + 27636 + 13803
    apply_event(st, _message_start(5, 100, 50_000))
    assert st.context_tokens == 50_105  # the latest call, not a sum


def test_a_subagent_message_start_leaves_the_context_alone():
    st = TurnState()
    apply_event(st, _message_start(5, 100, 50_000))
    apply_event(st, _message_start(1, 1, 1, parent="toolu_1"))
    assert st.context_tokens == 50_105


def test_the_context_window_comes_from_the_result_model_usage():
    ev = fixture_events()["result"]
    assert result_context_window(ev, "claude-haiku-4-5-20251001") == 200_000
    assert result_context_window(ev, "") == 200_000  # no model set: the largest entry
    both = {"modelUsage": {"a": {"contextWindow": 200_000}, "opus-x": {"contextWindow": 10**6}}}
    assert result_context_window(both, "opus-x[1m]") == 10**6
    assert result_context_window({"modelUsage": {}}, "x") is None


def test_quota_comes_from_unified_windows():
    windows = rate_limit_windows(fixture_events()["rate_limit_event"])
    assert windows == {"status": "allowed", "five_hour": 0.01, "seven_day": 0}
    assert quota_part(windows) == "5h 1% · 7d 0%"


def test_a_status_other_than_allowed_leads_with_a_warning():
    assert quota_part({"status": "allowed_warning", "five_hour": 0.91}) == "⚠️ 5h 91%"
    assert quota_part({"status": "rejected", "five_hour": 1, "seven_day": 0.4}) == (
        "⚠️ 5h 100% · 7d 40%"
    )


def _usage_window(c, tmp_path, thread=555, model=None):
    t = window(c, tmp_path, thread)
    t.model = model
    return t


def test_the_summary_line_carries_context_and_quota(tmp_path):
    async def scenario():
        c = client(tmp_path)
        t = _usage_window(c, tmp_path)
        t._open_holder()
        await drain(c, t)
        events = fixture_events()
        t.proc = FakeProc(
            [
                line(
                    {
                        "type": "assistant",
                        "message": {
                            "content": [
                                {
                                    "type": "tool_use",
                                    "id": "t1",
                                    "name": "Bash",
                                    "input": {"command": "ls"},
                                }
                            ]
                        },
                    }
                ),
                line(events["message_start"]),
                line(events["rate_limit_event"]),
                line(events["result"]),
            ]
        )
        t.pending_writes = []
        await asyncio.wait_for(t._reader(), 5)
        await drain(c, t)
        summary = texts(c)[0].replace("\\", "").splitlines()[0]
        assert summary.startswith("🔧 1 · ")
        assert summary.endswith(" tokens · ctx 21% (41.4k) · 5h 1% · 7d 0%")
        assert t.context_window == 200_000
        assert t.saved["session"]["context_window"] == 200_000  # kept per window

    asyncio.run(scenario())


def test_a_1m_model_assumes_a_1m_window_before_any_result(tmp_path):
    async def scenario():
        c = client(tmp_path)
        t = _usage_window(c, tmp_path, model="claude-opus-x[1m]")
        assert t._usage_parts(100_000) == ["ctx 10% (100.0k)"]
        t.model = "claude-sonnet-x"
        assert t._usage_parts(100_000) == ["ctx 50% (100.0k)"]

    asyncio.run(scenario())


def test_the_live_status_line_carries_context_and_quota(tmp_path):
    async def scenario():
        c = client(tmp_path)
        t = _usage_window(c, tmp_path)
        t._open_holder()
        events = fixture_events()
        apply_event(t.turn, events["message_start"])
        t._note_quota(events["rate_limit_event"])
        head = t._live_view(t.turn).splitlines()[0]
        assert head.endswith("tokens · ctx 21% (41.4k) · 5h 1% · 7d 0%)")

    asyncio.run(scenario())


def test_quota_is_shared_by_the_windows_on_one_account(tmp_path):
    async def scenario():
        c = client(tmp_path)
        first = _usage_window(c, tmp_path, thread=555)
        second = _usage_window(c, tmp_path, thread=556)
        other = _usage_window(c, tmp_path, thread=557)
        other.config_dir = str(tmp_path / ".claude-other")
        first._note_quota(fixture_events()["rate_limit_event"])
        assert second._usage_parts(0) == ["5h 1% · 7d 0%"]  # same account: shown
        assert other._usage_parts(0) == []  # another account: its own quota

    asyncio.run(scenario())


def test_no_separate_quota_alert_message_is_sent(tmp_path):
    async def scenario():
        c = client(tmp_path)
        t = _usage_window(c, tmp_path)
        hot = copy.deepcopy(fixture_events()["rate_limit_event"])
        hot["rate_limit_info"]["unifiedWindows"]["five_hour"]["utilization"] = 0.97
        t._open_holder()
        await drain(c, t)
        t.proc = FakeProc([line(hot), result("ok")])
        t.pending_writes = []
        await asyncio.wait_for(t._reader(), 5)
        await drain(c, t)
        assert not any("quota" in text for text in texts(c))
        assert "5h 97%" in texts(c)[0]

    asyncio.run(scenario())
