"""Pure parsing of the Claude CLI stream-json / transcript events: the Claude backend's
translation of its stream into the agent core's neutral events.

Transport-agnostic and side-effect-free, so it can be tested against fixed inputs.
"""

from __future__ import annotations

import json

from tgforge.plugins.agent.render import tool_line
from tgforge.plugins.agent.session import (
    BlockDone,
    Compacted,
    MessageDone,
    StepStart,
    TextDelta,
    ThoughtDelta,
    ToolStart,
    TurnState,
    Usage,
    apply,
)

_STEP_START = StepStart()
_MESSAGE_DONE = MessageDone()
_COMPACTED = Compacted()


def turn_events(ev: dict) -> list:
    """One stream-json event → the neutral turn events it carries: stream deltas, the
    token roll and context size of a model call, completed text/thinking blocks, the
    message's tool line, and the compaction flag."""
    t = ev.get("type")
    if t == "stream_event":
        inner = ev.get("event", {})
        it = inner.get("type")
        if it == "content_block_delta":
            delta = inner.get("delta", {})
            dt = delta.get("type")
            if dt == "text_delta":
                return [TextDelta(delta.get("text", ""))]
            if dt == "thinking_delta":
                return [ThoughtDelta(delta.get("text", ""))]
            return []
        if it == "message_start":
            usage = inner.get("message", {}).get("usage") or {}
            if usage and ev.get("parent_tool_use_id") is None:  # the main agent, not a subagent
                used = (
                    (usage.get("input_tokens") or 0)
                    + (usage.get("cache_creation_input_tokens") or 0)
                    + (usage.get("cache_read_input_tokens") or 0)
                )
                return [_STEP_START, Usage(context_used=used)]
            return [_STEP_START]
        if it == "message_delta":
            usage = inner.get("usage") or {}
            return [Usage(output_tokens=usage.get("output_tokens"))]
        return []
    if t == "assistant":
        # each block is its own assistant message; completed text/thinking in order,
        # then the live tails clear, then the message's (first) tool line
        out: list = []
        for b in ev.get("message", {}).get("content", []):
            if not isinstance(b, dict):
                continue
            if b.get("type") == "text":
                out.append(BlockDone("text", b.get("text") or ""))
            elif b.get("type") == "thinking":
                out.append(BlockDone("thinking", b.get("thinking") or ""))
        out.append(_MESSAGE_DONE)
        for b in ev.get("message", {}).get("content") or []:
            if b.get("type") == "tool_use":
                out.append(ToolStart(b.get("id") or "", b.get("name", "tool"), b.get("input")))
                break
        return out
    if t == "system" and ev.get("subtype") == "compact_boundary":
        return [_COMPACTED]
    return []


def apply_event(st: TurnState, ev: dict) -> None:
    """Fold one stream-json event into the turn state."""
    for neutral in turn_events(ev):
        apply(st, neutral)


def mirror_parse(raw: str) -> tuple[list[str], list[str], list[tuple[str, str]], bool]:
    """New transcript lines -> ([user]/[cli] messages, tool detail lines,
    pasted images as (media_type, base64), whether the tail is mid-turn)."""
    out: list[str] = []
    tools: list[str] = []
    images: list[tuple[str, str]] = []
    in_flight = False
    for line in raw.splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("isMeta"):
            continue
        t = ev.get("type")
        if t not in ("user", "assistant"):
            continue
        msg = ev.get("message")
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        texts: list[str] = []
        saw_tool = False
        if isinstance(content, str):
            texts.append(content)
        elif isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text":
                    texts.append(block.get("text", ""))
                elif block.get("type") == "tool_use":
                    tools.append(tool_line(block.get("name", "tool"), block.get("input")))
                    saw_tool = True
                elif block.get("type") == "tool_result":
                    saw_tool = True
                elif block.get("type") == "image":
                    src = block.get("source", {})
                    if src.get("type") == "base64" and src.get("data"):
                        images.append((src.get("media_type", "image/png"), src["data"]))
        body = "\n".join(s for s in texts if s.strip()).strip()
        if saw_tool or t == "user":
            in_flight = True  # tool activity, or a fresh prompt awaiting work
        elif t == "assistant" and body:
            in_flight = False  # text-only assistant message = turn finished
        if not body:
            continue
        # harness-injected wrappers (<command-…>, <task-notification>, …)
        # open with a tag; human input doesn't
        if body.startswith(("<", "Caveat:", "[SYSTEM NOTIFICATION")):
            continue
        prefix = "[user]" if t == "user" else "[cli]"
        out.append(f"{prefix} {body}")
    return out, tools, images, in_flight


def has_background_launch(ev: dict) -> bool:
    """An assistant event launching background work that outlives the turn: a
    Bash with run_in_background, or an Agent subagent (background by default)."""
    if ev.get("type") != "assistant":
        return False
    for b in ev.get("message", {}).get("content") or []:
        if not (isinstance(b, dict) and b.get("type") == "tool_use"):
            continue
        inp = b.get("input") if isinstance(b.get("input"), dict) else {}
        if b.get("name") == "Bash" and inp.get("run_in_background"):
            return True
        if b.get("name") == "Agent" and inp.get("run_in_background", True):
            return True
    return False


def is_prompt_replay(ev: dict) -> bool:
    """A replayed user prompt (has text), not a tool_result user event."""
    content = ev.get("message", {}).get("content")
    if isinstance(content, str):
        return True
    return isinstance(content, list) and any(
        isinstance(b, dict) and b.get("type") == "text" for b in content
    )


def prompt_block_count(ev: dict) -> int:
    """How many prompt writes a (possibly fold-merged) user replay carries:
    a message folded mid-turn is merged in as an extra text block, so one
    replay event can stand for several stdin writes."""
    content = ev.get("message", {}).get("content")
    if isinstance(content, str):
        return 1
    if isinstance(content, list):
        return sum(1 for b in content if isinstance(b, dict) and b.get("type") == "text")
    return 0


def is_notification_result(ev: dict) -> bool:
    """A `result` closing the CLI's own turn for a background-task notification."""
    origin = ev.get("origin")
    return isinstance(origin, dict) and origin.get("kind") == "task-notification"


def extract_tool_status(ev: dict) -> str | None:
    t = ev.get("type")
    if t == "assistant" and ev.get("message", {}).get("content"):
        for block in ev["message"]["content"]:
            if block.get("type") == "tool_use":
                return tool_line(block.get("name", "tool"), block.get("input"))
    return None


def rate_limit_windows(ev: dict) -> dict:
    """A `rate_limit_event` → {"status", "five_hour", "seven_day"} with each window's
    utilization (0–1) from `rate_limit_info.unifiedWindows`; absent fields are left out."""
    info = ev.get("rate_limit_info") or {}
    out: dict = {}
    if isinstance(info.get("status"), str):
        out["status"] = info["status"]
    windows = info.get("unifiedWindows") or {}
    for key in ("five_hour", "seven_day"):
        util = (windows.get(key) or {}).get("utilization")
        if isinstance(util, (int, float)):
            out[key] = util
    return out


def result_context_window(ev: dict, model: str) -> int | None:
    """The context window a `result` reports in `modelUsage`: the entry naming the
    window's model (any `[1m]` tag and date suffix aside), else the largest one."""
    usage = ev.get("modelUsage") or {}
    windows = {
        name: entry["contextWindow"]
        for name, entry in usage.items()
        if isinstance(entry, dict) and isinstance(entry.get("contextWindow"), int)
    }
    if not windows:
        return None
    wanted = model.removesuffix("[1m]")
    for name, window in windows.items():
        if wanted and (wanted.startswith(name) or name.startswith(wanted)):
            return window
    return max(windows.values())
