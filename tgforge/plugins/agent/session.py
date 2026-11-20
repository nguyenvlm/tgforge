"""The turn engine: the neutral events every agent backend emits, a pure reducer of
the turn events into `TurnState`, and the finalize answer-selection. Backend- and
transport-agnostic: each backend turns its CLI's output into these events, and the
window's async lifecycle calls in here, so turn shaping is testable without a process
or a platform.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from tgforge.plugins.agent.render import extract_attachments, extract_suggestions, tool_line

# ── Neutral events ─────────────────────────────────────────────────
# Turn events (folded into `TurnState` by `apply`):


@dataclass(slots=True)
class TextDelta:
    """A piece of the answer text being streamed."""

    text: str


@dataclass(slots=True)
class ThoughtDelta:
    """A piece of the model's thinking being streamed."""

    text: str


@dataclass(slots=True)
class StepStart:
    """A new model call starts: its output tokens count from zero."""


@dataclass(slots=True)
class BlockDone:
    """A finished `text` or `thinking` block. `text` None: the block is what was
    streamed; a thinking block that streamed deltas is always taken from them."""

    kind: str
    text: str | None = None


@dataclass(slots=True)
class MessageDone:
    """The model's message ended: the live text and thinking tails are cleared."""


@dataclass(slots=True)
class ToolStart:
    """A tool call begins."""

    id: str
    name: str
    input: dict | str | None


@dataclass(slots=True)
class ToolEnd:
    """A tool call ends: `status` is `completed` or `failed`; `output` its text."""

    id: str
    status: str
    output: str = ""


@dataclass(slots=True)
class Usage:
    """The context used and its size after a model call, and the call's output
    tokens; None leaves a field as it was."""

    context_used: int | None = None
    context_size: int | None = None
    output_tokens: int | None = None


@dataclass(slots=True)
class TokenTotal:
    """The turn's output tokens as the CLI counted them at its end (replaces the
    per-step sum)."""

    tokens: int


@dataclass(slots=True)
class Compacted:
    """The session's context was compacted during the turn."""


# Window events (handled by the window, `AgentTopic.on_event`):


@dataclass(slots=True)
class JobStarted:
    """A background job starts. `path`: the file its output goes to, when it has one."""

    id: str
    label: str
    path: str | None = None


@dataclass(slots=True)
class JobEnded:
    """A background job ends with its outcome mark: ✓ done, ✗ failed, ◼ unknown."""

    id: str
    mark: str


@dataclass(slots=True)
class Title:
    """The session's title."""

    text: str


@dataclass(slots=True)
class PromptFolded:
    """The queued prompt `prompt_id` joined the running turn."""

    prompt_id: str


TURN_EVENTS = (
    TextDelta,
    ThoughtDelta,
    StepStart,
    BlockDone,
    MessageDone,
    ToolStart,
    ToolEnd,
    Usage,
    TokenTotal,
    Compacted,
)


@dataclass
class TurnState:
    """Per-turn accumulators, reset at each turn start."""

    card_id: int | None = None  # the turn's live card message, once the driver sent it
    reply_to: int | None = None  # the message the turn answers
    started: float = 0.0  # monotonic turn start
    context_tokens: int = 0  # prompt size of the turn's latest model call
    preview_parts: list[str] = field(default_factory=list)  # current text block, live
    thinking_parts: list[str] = field(default_factory=list)  # current think block, live
    # ordered completed events (kind, payload): "tool"/"thinking"/"text";
    # the last "text" entry is the turn's answer, split out at finalize.
    timeline: list[tuple[str, str]] = field(default_factory=list)
    tok_base: int = 0
    tok_latest: int = 0
    compacted: bool = False
    awaits_replay: bool = False  # opened for a user prompt whose replay has not come
    prompt_id: str | None = None  # the prompt the turn runs under now (its newest)
    tool_entries: dict[str, int] = field(default_factory=dict)  # tool id → timeline index
    _think_delta_seen: bool = False  # did the current message stream thinking deltas


@dataclass
class TurnAnswer:
    text: str
    options: list[str]  # [[suggest]] quick replies
    attachments: list[str]  # [[attach]] file paths
    completed: bool  # a real result (vs cancel/error/compaction fallback)


def apply(st: TurnState, ev) -> None:
    """Fold one turn event into the turn state."""
    kind = type(ev)
    if kind is TextDelta:
        st.preview_parts.append(ev.text)
    elif kind is ThoughtDelta:
        st.thinking_parts.append(ev.text)
        st._think_delta_seen = True
    elif kind is StepStart:
        st._think_delta_seen = False
        st.tok_base += st.tok_latest
        st.tok_latest = 0
    elif kind is Usage:
        if ev.context_used is not None:
            st.context_tokens = ev.context_used
        if ev.output_tokens is not None:
            st.tok_latest = ev.output_tokens or st.tok_latest
    elif kind is BlockDone:
        if ev.kind == "thinking" and (st._think_delta_seen or ev.text is None):
            text = "".join(st.thinking_parts)
        elif ev.text is None:
            text = "".join(st.preview_parts)
        else:
            text = ev.text
        text = text.strip()
        if text:
            st.timeline.append((ev.kind, text))
    elif kind is MessageDone:
        st.preview_parts = []
        st.thinking_parts = []
    elif kind is ToolStart:
        st.tool_entries[ev.id] = len(st.timeline)
        st.timeline.append(("tool", tool_line(ev.name, ev.input)))
    elif kind is ToolEnd:
        index = st.tool_entries.get(ev.id)
        if ev.status == "failed" and index is not None:
            line = st.timeline[index][1]
            st.timeline[index] = ("tool", f"{line}\n✗ {ev.output}" if ev.output else f"{line} ✗")
    elif kind is TokenTotal:
        st.tok_base, st.tok_latest = ev.tokens, 0
    elif kind is Compacted:
        st.compacted = True  # empty result = compaction, not cancel


def last_text(st: TurnState) -> str:
    """The turn's answer when its backend sends none apart: the last timeline entry
    when it is a text block, else ""."""
    if st.timeline and st.timeline[-1][0] == "text":
        return st.timeline[-1][1]
    return ""


def finalize_answer(
    st: TurnState,
    result_text: str,
    subtype: str = "success",
    is_error: bool = False,
    cancel_requested: bool = False,
    *,
    agent: str,
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
        answer = f"⚠️ {agent} ended with an error ({subtype}). Send again to retry."
    elif st.compacted:
        answer = "(context compacted)"
    else:
        answer = "(no reply)"
    answer, options = extract_suggestions(answer)
    answer, attachments = extract_attachments(answer)
    return TurnAnswer(answer, options, attachments, completed)
