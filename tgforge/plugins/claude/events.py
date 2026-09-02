"""Pure parsing of the Claude CLI stream-json / transcript events.

Transport-agnostic and side-effect-free, so it can be tested against fixed inputs.
"""

from __future__ import annotations

import json

from tgforge.plugins.claude.render import tool_line


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


def extract_tool_status(ev: dict) -> str | None:
    t = ev.get("type")
    if t == "assistant" and ev.get("message", {}).get("content"):
        for block in ev["message"]["content"]:
            if block.get("type") == "tool_use":
                return tool_line(block.get("name", "tool"), block.get("input"))
    return None
