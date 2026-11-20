"""The agy backend's translation: ACP `session/update` payloads and transcript steps →
the agent core's neutral events. Pure functions over an explicit `StreamState`, so the
rules are testable against the captured payloads without a process.

Turn boundaries (the window applies them, `driver.py`):
- a prompted turn ends on the `session/prompt` reply of its newest prompt;
- an earlier prompt's reply while a newer one is outstanding is the fold signal: the
  next prompt joined the running turn;
- a `run_command` tool call still running when its turn ends is a background job; its
  `tool_call_update` `completed` / `failed` ends it and starts a wake turn;
- a wake turn (no prompt) ends on the next transcript step that is a `PLANNER_RESPONSE`
  with no `tool_calls`, or an error step (`ERROR_MESSAGE`, or a step whose status is
  `ERROR`).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from tgforge.plugins.agent.render import tool_line
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
    Usage,
)

JOB_TOOL = "run_command"  # the tool whose call can outlive its turn
_ENDED = ("completed", "failed")
_USER_REQUEST_RE = re.compile(r"<USER_REQUEST>\n?(.*?)\n?</USER_REQUEST>", re.DOTALL)
DENIED = "denied: the window answers every permission ask with deny"


@dataclass
class StreamState:
    """What the translation keeps between updates: the block being streamed
    (`text` / `thinking`), every tool call seen (id → name, label, command, status,
    whether it is a job), and the ids of the tool calls the window denied."""

    live: str | None = None
    tools: dict[str, dict] = field(default_factory=dict)
    denied: set[str] = field(default_factory=set)


def _content_text(update: dict) -> str:
    content = update.get("content")
    return content.get("text", "") if isinstance(content, dict) else ""


def close_block(state: StreamState) -> list:
    """End the block being streamed, if any."""
    if state.live is None:
        return []
    kind, state.live = state.live, None
    return [BlockDone(kind), MessageDone()]


def _stream(state: StreamState, kind: str, event) -> list:
    out = close_block(state) if state.live not in (None, kind) else []
    state.live = kind
    out.append(event)
    return out


def _tool_output(update: dict) -> str:
    raw = update.get("rawOutput")
    if isinstance(raw, dict) and isinstance(raw.get("output"), str):
        return raw["output"].strip()
    parts = [
        c["content"].get("text", "")
        for c in update.get("content") or []
        if isinstance(c, dict) and isinstance(c.get("content"), dict)
    ]
    return "\n".join(p for p in parts if p).strip()


def _tool_input(update: dict):
    raw = update.get("rawInput")
    return raw if isinstance(raw, dict) else None


def _tool_end(state: StreamState, tool_id: str, tool: dict, status: str, update: dict) -> list:
    tool["status"] = status
    if tool_id in state.denied:
        output = DENIED
    else:
        output = _tool_output(update) if status == "failed" else ""
    out = [ToolEnd(tool_id, status, output)]
    if tool["job"]:
        out.append(JobEnded(tool_id, "✓" if status == "completed" else "✗"))
    return out


def update_events(state: StreamState, update: dict) -> list:
    """One `session/update` `update` → its neutral events."""
    kind = update.get("sessionUpdate")
    if kind == "agent_message_chunk":
        return _stream(state, "text", TextDelta(_content_text(update)))
    if kind == "agent_thought_chunk":
        return _stream(state, "thinking", ThoughtDelta(_content_text(update)))
    if kind == "usage_update":
        return [Usage(context_used=update.get("used"), context_size=update.get("size"))]
    if kind == "session_info_update" and update.get("title"):
        return [Title(update["title"])]
    if kind not in ("tool_call", "tool_call_update"):
        return []
    tool_id = update.get("toolCallId") or ""
    status = update.get("status") or ""
    tool = state.tools.get(tool_id)
    out: list = []
    if tool is None:
        out += close_block(state)
        tool_input = _tool_input(update)
        args = tool_input if isinstance(tool_input, dict) else {}
        name = update.get("name") or update.get("title") or "tool"
        tool = {
            "name": name,
            "label": args.get("toolSummary") or update.get("title") or name,
            "command": args.get("CommandLine"),
            "status": status,
            "job": False,
        }
        state.tools[tool_id] = tool
        out.append(ToolStart(tool_id, name, tool_input))
    if status in _ENDED and tool["status"] not in _ENDED:
        out += _tool_end(state, tool_id, tool, status, update)
    elif status:
        tool["status"] = status
    return out


def running_jobs(state: StreamState) -> list:
    """At a turn's end: every `run_command` call still running becomes a job."""
    out = []
    for tool_id, tool in state.tools.items():
        if tool["name"] == JOB_TOOL and not tool["job"] and tool["status"] not in _ENDED:
            tool["job"] = True
            out.append(JobStarted(tool_id, tool["label"]))
    return out


def forget_ended(state: StreamState) -> None:
    """At a turn's end: drop every ended tool call, a job whose end has arrived included
    (a running job stays until its own end)."""
    state.tools = {
        tool_id: tool for tool_id, tool in state.tools.items() if tool["status"] not in _ENDED
    }
    state.denied &= set(state.tools)


# ── Transcript steps ───────────────────────────────────────────────


def parse_steps(raw: str) -> list[dict]:
    """New transcript lines → their steps (a line that is not JSON is skipped)."""
    steps = []
    for line in raw.splitlines():
        try:
            step = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(step, dict):
            steps.append(step)
    return steps


def step_events(step: dict) -> list:
    """A model step's output tokens (the step's own count; ACP sends none per step)."""
    if step.get("type") == "PLANNER_RESPONSE" and isinstance(step.get("output_tokens"), int):
        return [StepStart(), Usage(output_tokens=step["output_tokens"])]
    return []


def wake_end(step: dict) -> str | None:
    """The turn end a step marks when no prompt is outstanding: `end_turn` for a
    `PLANNER_RESPONSE` with no `tool_calls`, `error` for an error step, else None."""
    if step.get("type") == "ERROR_MESSAGE" or step.get("status") == "ERROR":
        return "error"
    if step.get("type") == "PLANNER_RESPONSE" and not step.get("tool_calls"):
        return "end_turn"
    return None


def _decoded_args(args) -> dict:
    """A transcript tool call's args, each value JSON-decoded when it is JSON text."""
    if not isinstance(args, dict):
        return {}
    out = {}
    for key, value in args.items():
        try:
            out[key] = json.loads(value) if isinstance(value, str) else value
        except json.JSONDecodeError:
            out[key] = value
    return out


def mirror_parse(raw: str) -> tuple[list[str], list[str], list[tuple[str, str]], bool]:
    """New transcript lines -> ([user]/[cli] messages, tool lines, images (none: the
    transcript holds no image data), whether the tail is mid-turn)."""
    out: list[str] = []
    tools: list[str] = []
    in_flight = False
    for step in parse_steps(raw):
        kind = step.get("type")
        if kind == "USER_INPUT":
            match = _USER_REQUEST_RE.search(step.get("content") or "")
            text = (match.group(1) if match else step.get("content") or "").strip()
            if text:
                out.append(f"[user] {text}")
            in_flight = True
        elif kind == "PLANNER_RESPONSE":
            calls = step.get("tool_calls") or []
            for call in calls:
                tools.append(tool_line(call.get("name", "tool"), _decoded_args(call.get("args"))))
            text = (step.get("content") or "").strip()
            if text and not calls:
                out.append(f"[cli] {text}")
            in_flight = bool(calls)
        elif wake_end(step) == "error":
            in_flight = False
    return out, tools, [], in_flight


# ── Quota (`agy --output-format json -p=/usage`) ───────────────────

_GROUP_MODELS = "Models within this group:"
_WINDOWS = {"5h": "five_hour", "weekly": "seven_day"}


def usage_groups(payload: dict) -> list[dict]:
    """The `/usage` JSON's quota groups (`command.data.groups`), or [] when absent."""
    data = (payload.get("command") or {}).get("data") or {}
    groups = data.get("groups")
    return [g for g in groups if isinstance(g, dict)] if isinstance(groups, list) else []


_EFFORT_SUFFIX = re.compile(r"\s*\((?:Low|Medium|High|XHigh)\)$")


def _group_models(group: dict) -> list[str]:
    """The model names a group's description lists (`Models within this group: a, b`)."""
    text = group.get("description") or ""
    if _GROUP_MODELS not in text:
        return []
    listed = text.split(_GROUP_MODELS, 1)[1]
    return [name.strip() for name in listed.split(",") if name.strip()]


def model_quota(groups: list[dict], model_name: str | None) -> dict | None:
    """The quota of the group naming `model_name` (a model option's display name) — by its
    own name or in its description's model list, the name as given or without a trailing
    effort label (`GPT-OSS 120B (Medium)` is listed as `GPT-OSS 120B`) — as the
    status-line quota (`five_hour` / `seven_day` used fractions, `status` `limited` when
    a bucket has nothing left); None when no group names the model."""
    names = [model_name, _EFFORT_SUFFIX.sub("", model_name)] if model_name else []
    group = next(
        (g for name in names for g in groups if g.get("name") == name or name in _group_models(g)),
        None,
    )
    if group is None:
        return None
    out: dict = {"status": "allowed"}
    for bucket in group.get("buckets") or []:
        key = _WINDOWS.get(bucket.get("window"))
        remaining = bucket.get("remaining_fraction")
        if key is None or not isinstance(remaining, (int, float)):
            continue
        out[key] = 1 - remaining
        if remaining <= 0:
            out["status"] = "limited"
    return out
