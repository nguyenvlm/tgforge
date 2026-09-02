"""The turn engine: a pure reducer over the Claude CLI stream-json events, plus
the finalize answer-selection. Transport-agnostic — the async process lifecycle
(spawn, stdin folding, holder reordering) is the driver's job and calls in here,
so the turn-shaping logic is unit-testable without a process or Telegram.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from tgforge.plugins.claude.events import extract_tool_status


@dataclass
class TurnState:
    """Per-turn accumulators, reset at each turn start."""

    preview_parts: list[str] = field(default_factory=list)  # current text block, live
    thinking_parts: list[str] = field(default_factory=list)  # current think block, live
    # ordered completed events (kind, payload): "tool"/"thinking"/"text";
    # the last "text" entry is the turn's answer, split out at finalize.
    timeline: list[tuple[str, str]] = field(default_factory=list)
    tok_base: int = 0
    tok_latest: int = 0
    compacted: bool = False
    _think_delta_seen: bool = False  # did the current message stream thinking deltas


@dataclass
class TurnAnswer:
    text: str
    options: list[str]  # [[suggest]] quick replies
    attachments: list[str]  # [[attach]] file paths
    completed: bool  # a real result (vs cancel/error/compaction fallback)


def apply_event(st: TurnState, ev: dict) -> None:
    """Fold one stream-json event into the turn state: stream deltas, token roll,
    completed text/thinking blocks, tool lines, and the compaction flag."""
    t = ev.get("type")
    if t == "system" and ev.get("subtype") == "compact_boundary":
        st.compacted = True  # empty result = compaction, not cancel
    if t == "stream_event":
        inner = ev.get("event", {})
        it = inner.get("type")
        if it == "content_block_delta":
            delta = inner.get("delta", {})
            dt = delta.get("type")
            if dt == "text_delta":
                st.preview_parts.append(delta.get("text", ""))
            elif dt == "thinking_delta":
                st.thinking_parts.append(delta.get("text", ""))
                st._think_delta_seen = True
        elif it == "message_start":
            st._think_delta_seen = False
            st.tok_base += st.tok_latest
            st.tok_latest = 0
        elif it == "message_delta":
            usage = inner.get("usage") or {}
            st.tok_latest = usage.get("output_tokens") or st.tok_latest
    elif t == "assistant":
        # each block is its own assistant message; append completed text/thinking
        # in order (tool_use is caught below), then clear the live-tail buffers.
        for b in ev.get("message", {}).get("content", []):
            if not isinstance(b, dict):
                continue
            if b.get("type") == "text":
                txt = (b.get("text") or "").strip()
                if txt:
                    st.timeline.append(("text", txt))
            elif b.get("type") == "thinking":
                txt = (
                    "".join(st.thinking_parts)
                    if st._think_delta_seen
                    else (b.get("thinking") or "")
                ).strip()
                if txt:
                    st.timeline.append(("thinking", txt))
        st.preview_parts = []
        st.thinking_parts = []
    ts = extract_tool_status(ev)
    if ts:
        st.timeline.append(("tool", ts))


def finalize_answer(
    st: TurnState,
    result_text: str,
    subtype: str = "success",
    is_error: bool = False,
    cancel_requested: bool = False,
) -> TurnAnswer:
    """Pick the turn's answer from the result event and streamed tails. An empty
    result is ambiguous: name the cause instead of always '(cancelled)'; partial
    streamed text wins over a label. Flushes an in-progress thinking block into the
    timeline as a side effect."""
    tail_think = "".join(st.thinking_parts).strip()
    tail_text = "".join(st.preview_parts).strip()
    if tail_think:
        st.timeline.append(("thinking", tail_think))
    completed = bool(result_text.strip())
    if completed:
        answer = result_text
    elif tail_text:
        answer = tail_text
    elif cancel_requested:
        answer = "(cancelled)"
    elif is_error or subtype in ("error_during_execution", "error_max_turns"):
        answer = f"⚠️ Claude ended with an error ({subtype}). Send again to retry."
    elif st.compacted:
        answer = "(context compacted)"
    else:
        answer = "(no reply)"
    from tgforge.plugins.claude.render import extract_attachments, extract_suggestions

    answer, options = extract_suggestions(answer)
    answer, attachments = extract_attachments(answer)
    return TurnAnswer(answer, options, attachments, completed)
