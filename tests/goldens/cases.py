"""Shared differential-test inputs for the pure render/parse layer.

Not a test. `generate.py` runs the REFERENCE bot's `_<name>` originals over these
cases; `../test_render_parity.py` runs the tgforge functions over the same inputs.
Both import this module so the input set never drifts between oracle and test —
and it holds pure data only (no tgforge, no reference imports), so either env can
load it.

Each case is (name, args). `name` maps to the reference `_<name>` and to the
tgforge callable of the same name.
"""

from __future__ import annotations

import json

# A recorded-shape transcript covering every branch mirror_parse walks.
_TRANSCRIPT_LINES = [
    {"type": "user", "message": {"content": "hello there"}},
    {"type": "assistant", "message": {"content": [{"type": "text", "text": "hi back"}]}},
    {
        "type": "assistant",
        "message": {
            "content": [{"type": "tool_use", "name": "Read", "input": {"file_path": "/x/io.py"}}]
        },
    },
    {"type": "user", "message": {"content": [{"type": "tool_result"}]}},
    {"isMeta": True, "type": "assistant", "message": {"content": "ignored meta"}},
    {
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": "<command-name>skip</command-name>"}]},
    },
    {
        "type": "user",
        "message": {
            "content": [
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"},
                }
            ]
        },
    },
    {"type": "assistant", "message": {"content": [{"type": "text", "text": "final answer"}]}},
]
_TRANSCRIPT = "\n".join(json.dumps(x, ensure_ascii=False) for x in _TRANSCRIPT_LINES)

_EV_TEXT = {"type": "user", "message": {"content": "just text"}}
_EV_TOOL_RESULT = {"type": "user", "message": {"content": [{"type": "tool_result"}]}}
_EV_MULTI = {
    "type": "user",
    "message": {"content": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]},
}
_EV_BG_BASH = {
    "type": "assistant",
    "message": {
        "content": [
            {
                "type": "tool_use",
                "name": "Bash",
                "input": {"command": "x", "run_in_background": True},
            }
        ]
    },
}
_EV_BG_AGENT = {
    "type": "assistant",
    "message": {"content": [{"type": "tool_use", "name": "Agent", "input": {}}]},
}
_EV_FG_BASH = {
    "type": "assistant",
    "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {"command": "x"}}]},
}
_EV_TOOL_USE = {
    "type": "assistant",
    "message": {"content": [{"type": "tool_use", "name": "Grep", "input": {"pattern": "foo"}}]},
}

RENDER_CASES = [
    ("chunks", [""]),
    ("chunks", ["short text"]),
    ("chunks", ["x" * 5000]),
    ("chunks", ["line\n" * 1500]),
    ("chunks", ["trailing\n\n"]),
    ("to_md", ["**bold** _italic_ `code`"]),
    ("to_md", ["# Heading\n- one\n- two"]),
    ("to_md", ["a [link](http://example.com) here"]),
    ("to_md", ["plain text, nothing special"]),
    ("mdv2_escape", ["a_b*c[d](e)"]),
    ("mdv2_escape", ["version 1.2.3!"]),
    ("mdv2_escape", ["back\\slash and #hash"]),
    ("expandable", ["Header only", ""]),
    ("expandable", ["Header", "line1\nline2\nline3"]),
    ("tool_line", ["Bash", {"command": "ls -la", "description": "list files"}]),
    ("tool_line", ["Bash", {"command": "ls -la"}]),
    ("tool_line", ["Read", {"file_path": "/a/b/io.py"}]),
    ("tool_line", ["Skill", {"skill": "audit"}]),
    ("tool_line", ["Grep", {"pattern": "foo.*bar"}]),
    ("tool_line", ["WebFetch", {"url": "http://x.com"}]),
    ("tool_line", ["Mystery", {"a": "", "b": "seen"}]),
    ("tool_line", ["Bash", "not-a-dict"]),
    ("tool_line", ["Bash", {"command": "x" * 200}]),
    ("fmt_tokens", [0]),
    ("fmt_tokens", [999]),
    ("fmt_tokens", [1000]),
    ("fmt_tokens", [15500]),
    ("shell_view", ["ls", ["out\n"], "done"]),
    ("shell_view", ["big", ["a" * 5000], "…"]),
    ("shell_view", ["cmd", [], ""]),
    ("shell_md", ["ls", ["hi there\n"], "done"]),
    ("shell_md", ["tall", ["x\n" * 30], "…"]),
    ("shell_md", ["long", ["a" * 700], "…"]),
    ("shell_md", ["bt", ["has `tick` and \\ slash\n"], "done"]),
    ("shell_md", ["cmd", [], ""]),
    ("strip_suggest", ["answer body\n[[suggest]] a | b | c"]),
    ("strip_suggest", ["text\n[[attach]] /p/one\nmore text"]),
    ("strip_suggest", ["no markers here"]),
    ("extract_attachments", ["ans\n[[attach]] /a | /b"]),
    ("extract_attachments", ["ans\n[[attach]] /a\n[[attach]] /b | /c"]),
    ("extract_attachments", ["no markers"]),
    ("extract_suggestions", ["ans\n[[suggest]] a | b | c | d | e | f | g"]),
    ("extract_suggestions", ["ans\n[[suggest]] only one"]),
    ("extract_suggestions", ["none here"]),
    (
        "render_timeline",
        [
            [
                ["tool", "💻 Bash: build"],
                ["tool", "💻 Bash: build"],
                ["thinking", "mulling it over"],
                ["text", "narration between"],
                ["text", "final answer\n[[suggest]] a | b"],
            ],
            True,
        ],
    ),
    (
        "render_timeline",
        [
            [
                ["tool", "📖 Read: io.py"],
                ["text", "only text"],
            ],
            False,
        ],
    ),
    ("render_timeline", [[], True]),
]

EVENT_CASES = [
    ("mirror_parse", [_TRANSCRIPT]),
    ("mirror_parse", [""]),
    ("mirror_parse", ["not json\n{bad}"]),
    ("has_background_launch", [_EV_BG_BASH]),
    ("has_background_launch", [_EV_BG_AGENT]),
    ("has_background_launch", [_EV_FG_BASH]),
    ("has_background_launch", [_EV_TEXT]),
    ("is_prompt_replay", [_EV_TEXT]),
    ("is_prompt_replay", [_EV_TOOL_RESULT]),
    ("is_prompt_replay", [_EV_MULTI]),
    ("prompt_block_count", [_EV_TEXT]),
    ("prompt_block_count", [_EV_MULTI]),
    ("prompt_block_count", [_EV_TOOL_RESULT]),
    ("extract_tool_status", [_EV_TOOL_USE]),
    ("extract_tool_status", [_EV_TEXT]),
]

ALL_CASES = [("render", name, args) for name, args in RENDER_CASES] + [
    ("event", name, args) for name, args in EVENT_CASES
]
