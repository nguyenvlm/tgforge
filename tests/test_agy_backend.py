"""The agy backend's pure translation over the captured payloads: ACP updates and
transcript steps → the core's neutral events, the job and wake-turn rules, and the
transcript mirror; plus the JSON-RPC client."""

from __future__ import annotations

import asyncio

import pytest
from agy_fakes import ACP, STEPS, FakeAgent, message, text_chunk

from tgforge.plugins.agent.session import (
    BlockDone,
    JobEnded,
    JobStarted,
    MessageDone,
    StepStart,
    TextDelta,
    ThoughtDelta,
    Title,
    ToolEnd,
    ToolStart,
    TurnState,
    Usage,
    apply,
    last_text,
)
from tgforge.plugins.agy import backend
from tgforge.plugins.agy.acp import AcpClient, AcpError


def _events(state, *messages):
    out = []
    for msg in messages:
        out += backend.update_events(state, msg["params"]["update"])
    return out


def test_chunks_stream_and_a_kind_switch_closes_the_block():
    state = backend.StreamState()
    events = _events(
        state,
        text_chunk("agent_thought_chunk", "think "),
        text_chunk("agent_thought_chunk", "more"),
        text_chunk("agent_message_chunk", "answer"),
    )
    assert events == [
        ThoughtDelta("think "),
        ThoughtDelta("more"),
        BlockDone("thinking"),
        MessageDone(),
        TextDelta("answer"),
    ]
    assert backend.close_block(state) == [BlockDone("text"), MessageDone()]
    assert backend.close_block(state) == []


def test_the_reducer_builds_the_timeline_and_the_answer_from_the_events():
    state, turn = backend.StreamState(), TurnState()
    for event in _events(
        state,
        text_chunk("agent_thought_chunk", "THINK"),
        message("tool_call_in_progress"),
        text_chunk("agent_message_chunk", "ANSWER"),
    ) + backend.close_block(state):
        apply(turn, event)
    assert turn.timeline == [
        ("thinking", "THINK"),
        ("tool", "🔧 run_command: sleep 6; echo hello-probe"),
        ("text", "ANSWER"),
    ]
    assert last_text(turn) == "ANSWER"


def test_a_tool_call_starts_once_and_its_end_closes_it():
    state = backend.StreamState()
    tool_id = ACP["job_tool_call"]["params"]["update"]["toolCallId"]
    started = _events(state, message("job_tool_call"), message("job_tool_call_pending"))
    assert [type(e) for e in started] == [ToolStart]
    assert started[0].id == tool_id and started[0].name == "run_command"
    assert _events(state, message("job_tool_call_completed")) == [ToolEnd(tool_id, "completed", "")]
    assert _events(state, message("job_tool_call_completed")) == []  # ended once


def test_a_run_command_still_running_at_turn_end_is_a_job_and_its_end_wakes():
    state = backend.StreamState()
    tool_id = ACP["job_tool_call"]["params"]["update"]["toolCallId"]
    _events(state, message("job_tool_call"), message("tool_call_in_progress", toolCallId="x"))
    _events(state, message("job_tool_call_completed", toolCallId="x"))
    assert backend.running_jobs(state) == [JobStarted(tool_id, "Start background sleep command")]
    assert backend.running_jobs(state) == []  # a job once
    backend.forget_ended(state)
    assert set(state.tools) == {tool_id}
    assert _events(state, message("job_tool_call_completed")) == [
        ToolEnd(tool_id, "completed", ""),
        JobEnded(tool_id, "✓"),
    ]
    backend.forget_ended(state)  # the next turn's end: the ended job's entry goes too
    assert state.tools == {}


def test_a_failed_job_ends_with_a_cross():
    state = backend.StreamState()
    _events(state, message("job_tool_call"))
    backend.running_jobs(state)
    tool_id = ACP["job_tool_call"]["params"]["update"]["toolCallId"]
    assert JobEnded(tool_id, "✗") in _events(
        state, message("tool_call_failed_denied", toolCallId=tool_id)
    )


def test_a_denied_tool_ends_failed_with_the_denial():
    state = backend.StreamState()
    update = ACP["tool_call_failed_denied"]["params"]["update"]
    state.denied.add(update["toolCallId"])
    _events(state, message("tool_call_in_progress", toolCallId=update["toolCallId"]))
    end = _events(state, message("tool_call_failed_denied"))
    assert end == [ToolEnd(update["toolCallId"], "failed", backend.DENIED)]
    turn = TurnState()
    apply(turn, ToolStart(update["toolCallId"], "run_command", update["rawInput"]))
    apply(turn, end[0])
    assert turn.timeline == [
        ("tool", f"🔧 run_command: touch c7-file && ls -l c7-file\n✗ {backend.DENIED}")
    ]


def test_usage_and_title_updates():
    state = backend.StreamState()
    assert _events(state, message("usage_update")) == [Usage(context_used=0, context_size=1048576)]
    assert _events(state, message("session_info_update")) == [Title("Run Command and Reply")]
    assert _events(state, message("user_message_chunk")) == []


def test_the_wake_turn_end_marker():
    assert backend.wake_end(STEPS["planner_final"]) == "end_turn"
    assert backend.wake_end(STEPS["planner_tool_calls"]) is None
    assert backend.wake_end(STEPS["system_task_finished"]) is None
    assert backend.wake_end(STEPS["system_reminder"]) is None
    assert backend.wake_end(STEPS["generic_running"]) is None
    assert backend.wake_end(STEPS["generic_error_denied"]) == "error"
    assert backend.wake_end({"type": "ERROR_MESSAGE", "status": "DONE"}) == "error"


def test_a_model_step_carries_its_output_tokens():
    assert backend.step_events(STEPS["planner_tool_calls"]) == [
        StepStart(),
        Usage(output_tokens=41),
    ]
    assert backend.step_events(STEPS["user_input"]) == []
    turn = TurnState()
    for s in (STEPS["planner_tool_calls"], STEPS["planner_final"]):
        for event in backend.step_events(s):
            apply(turn, event)
    assert turn.tok_base + turn.tok_latest == 44


def test_the_mirror_reads_the_transcript():
    import json

    raw = "\n".join(
        json.dumps(STEPS[k]) for k in ("user_input", "planner_tool_calls", "generic_running")
    )
    texts, tools, images, in_flight = backend.mirror_parse(raw + "\n")
    assert texts[0].startswith("[user] Start the shell command `sleep 8; echo bg-1`")
    assert tools == ["🔧 run_command: sleep 8; echo bg-1"]  # the args' JSON text decoded
    assert images == [] and in_flight
    texts, _, _, in_flight = backend.mirror_parse(json.dumps(STEPS["planner_final"]) + "\n")
    assert texts == ["[cli] started 1"] and not in_flight


def test_parse_steps_skips_a_partial_line():
    assert backend.parse_steps('{"type": "X"}\nnot json\n') == [{"type": "X"}]


# ── the JSON-RPC client ────────────────────────────────────────────


class _Handler:
    def __init__(self):
        self.seen = []

    async def on_reply(self, request_id, method, result, error):
        self.seen.append(("reply", request_id, method, result, error))

    async def on_notification(self, method, params):
        self.seen.append(("notification", method, params))

    async def on_request(self, request_id, method, params):
        self.seen.append(("request", request_id, method))


def test_the_client_pairs_replies_routes_notifications_and_answers_requests():
    async def scenario():
        agent, handler = FakeAgent(), _Handler()
        client = AcpClient(agent.stdout, agent.stdin, handler)
        reader = asyncio.create_task(client.run())
        result = await client.request("initialize", {"protocolVersion": 1})
        assert result == ACP["initialize_result"]["result"]
        assert agent.received[0] == {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": 1},
        }
        agent.send(message("usage_update"))
        agent.send(ACP["request_permission"])
        prompt_id, reply = await client.send_request("session/prompt", {}, wait=False)
        assert reply is None and prompt_id == "2"
        agent.send({"jsonrpc": "2.0", "id": 2, "result": {"stopReason": "end_turn"}})
        await client.respond("1", {"outcome": {"outcome": "cancelled"}})
        agent.exit()
        await reader
        kinds = [s[0] for s in handler.seen]
        assert kinds == ["reply", "notification", "request", "reply"]
        assert handler.seen[-1] == (
            "reply",
            "2",
            "session/prompt",
            {"stopReason": "end_turn"},
            None,
        )
        assert agent.received[-1] == {
            "jsonrpc": "2.0",
            "id": "1",
            "result": {"outcome": {"outcome": "cancelled"}},
        }

    asyncio.run(scenario())


def test_an_error_reply_raises_and_a_closed_stream_fails_the_waiting():
    async def scenario():
        agent = FakeAgent()
        client = AcpClient(agent.stdout, agent.stdin, _Handler())
        reader = asyncio.create_task(client.run())
        _id, reply = await client.send_request("session/new", {})  # answered from the capture
        await reply
        _id, pending = await client.send_request("session/prompt", {})
        agent.send({"jsonrpc": "2.0", "id": 2, "error": {"code": -32603, "message": "boom"}})
        _id, last = await client.send_request("x/y", {})
        with pytest.raises(AcpError, match="boom"):
            await asyncio.wait_for(pending, 1)
        agent.exit()
        await reader
        with pytest.raises(AcpError, match="ended"):
            await last

    asyncio.run(scenario())
