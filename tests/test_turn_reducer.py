"""Unit tests for the pure turn reducer (2c core). Replays a recorded-shape
stream-json turn and asserts the accumulated timeline, tokens, and answer.

Not differential (the reference reduction is inline in an async loop, not a
callable) — a straight unit test of the extracted logic. Live parity is 2g.
"""

from __future__ import annotations

from tgforge.plugins.claude.render import render_timeline
from tgforge.plugins.claude.session import TurnState, apply_event, finalize_answer


def _turn(events):
    st = TurnState()
    for ev in events:
        apply_event(st, ev)
    return st


def test_stream_deltas_accumulate_then_block_completes():
    st = _turn(
        [
            {"type": "stream_event", "event": {"type": "message_start"}},
            {
                "type": "stream_event",
                "event": {
                    "type": "content_block_delta",
                    "delta": {"type": "thinking_delta", "text": "pon"},
                },
            },
            {
                "type": "stream_event",
                "event": {
                    "type": "content_block_delta",
                    "delta": {"type": "thinking_delta", "text": "der"},
                },
            },
            {
                "type": "stream_event",
                "event": {"type": "message_delta", "usage": {"output_tokens": 42}},
            },
            {"type": "assistant", "message": {"content": [{"type": "thinking", "thinking": ""}]}},
        ]
    )
    # streamed thinking wins over the empty block payload; buffers cleared after
    assert st.timeline == [("thinking", "ponder")]
    assert st.thinking_parts == []
    assert st.tok_latest == 42


def test_token_roll_on_message_start():
    st = _turn(
        [
            {"type": "stream_event", "event": {"type": "message_start"}},
            {
                "type": "stream_event",
                "event": {"type": "message_delta", "usage": {"output_tokens": 100}},
            },
            {"type": "stream_event", "event": {"type": "message_start"}},
            {
                "type": "stream_event",
                "event": {"type": "message_delta", "usage": {"output_tokens": 30}},
            },
        ]
    )
    assert st.tok_base == 100
    assert st.tok_latest == 30


def test_tool_and_text_order_in_timeline():
    st = _turn(
        [
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "text", "text": "doing a thing"},
                        {"type": "tool_use", "name": "Read", "input": {"file_path": "/x/io.py"}},
                    ]
                },
            }
        ]
    )
    assert st.timeline == [("text", "doing a thing"), ("tool", "📖 Read: io.py")]


def test_finalize_completed_result_splits_markers():
    st = _turn(
        [{"type": "assistant", "message": {"content": [{"type": "text", "text": "the answer"}]}}]
    )
    ans = finalize_answer(st, "the answer\n[[suggest]] a | b\n[[attach]] /out/x.png")
    assert ans.completed is True
    assert ans.text == "the answer"
    assert ans.options == ["a", "b"]
    assert ans.attachments == ["/out/x.png"]


def test_finalize_empty_result_names_the_cause():
    assert finalize_answer(TurnState(), "", cancel_requested=True).text == "(cancelled)"
    assert finalize_answer(TurnState(compacted=True), "").text == "(context compacted)"
    assert finalize_answer(TurnState(), "").text == "(no reply)"
    assert finalize_answer(TurnState(), "", is_error=True).text.startswith("⚠️")


def test_finalize_partial_stream_beats_label():
    st = TurnState(preview_parts=["half written"])
    ans = finalize_answer(st, "", cancel_requested=True)
    assert ans.text == "half written"
    assert ans.completed is False


def test_reducer_feeds_render_timeline():
    st = _turn(
        [
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "narr"}]}},
            {
                "type": "assistant",
                "message": {
                    "content": [{"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}]
                },
            },
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "final"}]}},
        ]
    )
    lines, n_tools, n_narr = render_timeline(st.timeline, drop_last_text=True)
    assert n_tools == 1
    assert n_narr == 1
    assert lines == ["💬 narr", "💻 Bash: ls"]
