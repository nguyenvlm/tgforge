"""Unit tests for base render behavior that intentionally diverges from the
reference (so it is not in the differential goldens)."""

from __future__ import annotations

import pytest

from tgforge.base import ui
from tgforge.base.telegram import SAFE_RAW_CHUNK, TelegramAdapter, to_md
from tgforge.base.ui import MAX_MSG, fmt_duration


def md_chunks(text):
    """The Telegram split: pieces measured as their MarkdownV2 render."""
    return ui.md_chunks(text, TelegramAdapter.measure, SAFE_RAW_CHUNK)


def test_md_chunks_keep_converted_pieces_under_limit():
    # a dense, formatting-heavy reply: MarkdownV2 escaping expands it past a raw
    # split, which a plain-length chunker would truncate to invalid markup
    raw = "- `item_[i]` = a.b_c.d-e (x)*y_ ~z~ #tag\n" * 100
    pieces = md_chunks(raw)
    assert len(pieces) >= 2
    for p in pieces:
        assert len(to_md(p)) <= MAX_MSG  # the CONVERTED piece fits, not just the raw
    assert "".join(pieces).replace("\n", "") == raw.replace("\n", "")  # no content lost


@pytest.mark.parametrize(
    "seconds,expected",
    [
        (0, "0s"),
        (5, "5s"),
        (60, "1m"),
        (61, "1m 1s"),
        (3661, "1h 1m 1s"),
        (90000, "1d 1h"),
        (34300800, "1y 1mo 2d"),
    ],
)
def test_fmt_duration_full_units(seconds, expected):
    assert fmt_duration(seconds) == expected


def test_status_head_uses_new_duration():
    from tgforge.plugins.claude.render import status_head

    assert "1h" in status_head(0, 0, 3600, 0)


# ── to_md: GFM → MarkdownV2 layout ─────────────────────────────────

_MDV2_RESERVED = "_*[]()~`>#+-=|{}.!"
_MDV2_TOGGLES = ("__", "||", "*", "_", "~")


def assert_valid_mdv2(md: str) -> None:
    """Fail unless `md` parses as MarkdownV2: every reserved character outside code
    is escaped or opens/closes markup, and all markup is closed."""
    open_marks: list[str] = []
    in_link_text = 0
    i = 0
    line_start = True
    while i < len(md):
        ch = md[i]
        if ch == "\\":
            assert i + 1 < len(md), f"dangling backslash at {i}: {md!r}"
            assert md[i + 1] in _MDV2_RESERVED + "\\", f"bad escape at {i}: {md!r}"
            i += 2
            line_start = False
            continue
        fence = "```" if md.startswith("```", i) else "`" if ch == "`" else None
        if fence:
            end = i + len(fence)
            while not md.startswith(fence, end):
                assert end < len(md), f"unclosed {fence} at {i}: {md!r}"
                end += 2 if md[end] == "\\" else 1
            i = end + len(fence)
            line_start = False
            continue
        if line_start and (md.startswith("**>", i) or ch == ">"):
            i += 3 if md.startswith("**>", i) else 1
            line_start = False
            continue
        toggle = next((t for t in _MDV2_TOGGLES if md.startswith(t, i)), None)
        if toggle:
            if open_marks and open_marks[-1] == toggle:
                open_marks.pop()
            else:
                open_marks.append(toggle)
            i += len(toggle)
            line_start = False
            continue
        if ch == "[":
            in_link_text += 1
        elif ch == "]" and in_link_text:
            in_link_text -= 1
            assert md.startswith("](", i), f"link text without url at {i}: {md!r}"
            end = i + 2
            while md[end] != ")":
                end += 2 if md[end] == "\\" else 1
            i = end + 1
            line_start = False
            continue
        else:
            assert ch not in _MDV2_RESERVED, f"unescaped {ch!r} at {i}: {md!r}"
        line_start = ch == "\n"
        i += 1
    assert not open_marks and not in_link_text, f"unclosed markup {open_marks}: {md!r}"


@pytest.mark.parametrize(
    "bad",
    ["a.b", "1. one", "*open", "a_b", "x (y)", "`code", "[text", "a > b", "a || b"],
)
def test_mdv2_checker_rejects_invalid(bad):
    with pytest.raises(AssertionError):
        assert_valid_mdv2(bad)


TO_MD_CASES = [
    # inputs the reference-bot goldens used to pin
    ("**bold** _italic_ `code`", "*bold* _italic_ `code`"),
    ("# Heading\n- one\n- two", "📌 __*Heading*__\n◆ one\n◆ two\n"),
    ("a [link](http://example.com) here", "a [link](http://example.com) here"),
    ("plain text, nothing special", "plain text, nothing special"),
    # a list keeps the blank line before the next paragraph
    ("- a\n- b\n\nText.", "◆ a\n◆ b\n\nText\\."),
    ("1. a\n2. b\n\nAfter.", "1\\. a\n2\\. b\n\nAfter\\."),
    # a bullet list followed by a numbered list stays two lists
    ("- a\n\n1. one", "◆ a\n\n1\\. one\n"),
    # an unchecked task item is an empty box
    ("- [ ] todo\n- [x] done", "☐ todo\n✅ done\n"),
    # angle-bracket text is kept as literal, escaped text
    ("a <tag>", "a <tag\\>"),
    ("a <b>bold</b> & <tag>", "a <b\\>bold</b\\> & <tag\\>"),
    ("<div>\nhi\n</div>\n\nafter", "<div\\>\nhi\n</div\\>\n\nafter"),
    # a later paragraph of a list item sits right under its bullet, and a blank
    # line sets the item off from the next bullet
    ("- a\n\n  more of a\n- b", "◆ a\n  more of a\n\n◆ b\n"),
    ("- a\n\n  more of a\n\n- b", "◆ a\n  more of a\n\n◆ b\n"),
    ("1. a\n   b\n\n   c\n2. d", "1\\. a\n   b\n   c\n\n2\\. d\n"),
    # the bullet symbol follows the list depth and repeats past the third
    ("- a\n  - b\n    - c\n      - d\n- e", "◆ a\n    ▸ b\n        ⦁ c\n            ◆ d\n◆ e\n"),
    ("1. a\n   - b\n2. c", "1\\. a\n    ▸ b\n2\\. c\n"),
    # nesting levels are 4 spaces apart; a nested task item keeps its level's indent
    ("- [ ] t\n  - [x] x\n    - y", "☐ t\n    ✅ x\n        ⦁ y\n"),
    # items with one block each stay tight
    ("- a\n  - a1\n  - a2\n- b", "◆ a\n    ▸ a1\n    ▸ a2\n◆ b\n"),
    ("- a\n  b\n- c", "◆ a\n  b\n◆ c\n"),
    # the hanging indent never exceeds the source line's own indent
    ("- a\n  b\nc", "◆ a\n  b\nc\n"),
    ("- a\n\n  <div>\n  x\n  </div>\n- b", "◆ a\n  <div\\>\n  x\n  </div\\>\n\n◆ b\n"),
    ("- [ ] a\n\n  more", "☐ a\n  more\n"),
    ("- a\n  - b\n\n    more b\n\n  more a", "◆ a\n    ▸ b\n      more b\n\n  more a\n"),
    # a blockquote inside a list item sits under its bullet, its lines unindented
    ("- a\n\n  > q\n- b", "◆ a\n>q\n\n◆ b\n"),
    ("- a\n  > q1\n  > q2\n- b", "◆ a\n>q1\n>q2\n\n◆ b\n"),
    ("- a\n  > q1\n  > q2\n\n  more\n- b", "◆ a\n>q1\n>q2\n  more\n\n◆ b\n"),
    # a code block inside a list item starts on its own line; one newline before
    # the next bullet, since Telegram adds the gap after a code block itself
    ("- a\n  ```\n  code\n  ```\n- b", "◆ a\n```\ncode\n```\n◆ b\n"),
    # after a code block one newline is enough: Telegram adds the gap itself
    ("```py\nx = 1\n```\n\nAfter.", "```py\nx = 1\n```\nAfter\\."),
    ("```\na\n```\n\n```\nb\n```", "```\na\n```\n```\nb\n```"),
    ("| a |\n|---|\n| 1 |\n\nafter", "```\na\n-\n1\n```\nafter"),
    # code spans and code blocks come through unchanged
    ("run `a_b*c[d](e)` now", "run `a_b*c[d](e)` now"),
    ("use `<x>` and `a <b> c`", "use `<x>` and `a <b> c`"),
    ("```\n<x> a_b *c* .!\n  kept indent\n```", "```\n<x> a_b *c* .!\n  kept indent\n```"),
    ("- a\n  ```\n  <x> l1\n    l2\n  ```", "◆ a\n```\n<x> l1\n  l2\n```\n"),
]


@pytest.mark.parametrize("raw,expected", TO_MD_CASES)
def test_to_md_layout(raw, expected):
    assert to_md(raw) == expected


@pytest.mark.parametrize("raw", [raw for raw, _ in TO_MD_CASES])
def test_to_md_output_is_valid_mdv2(raw):
    assert_valid_mdv2(to_md(raw))


def test_md_chunks_fit_with_unindented_list_continuation():
    # lazy continuation lines inside a nested item: a hanging indent the source
    # lacks would grow every line past the SAFE_RAW_CHUNK bound
    raw = "- - - a\n" + ".\n" * 3000
    for p in md_chunks(raw):
        assert len(to_md(p)) <= MAX_MSG


def test_md_chunks_fit_with_deep_nesting():
    # 4 spaces per level against at least 2 in the source: still within 2x growth
    raw = "- a\n  - b\n    - c\n      - d\n        - e\n" * 300
    for p in md_chunks(raw):
        assert len(to_md(p)) <= MAX_MSG


def test_to_md_leaves_the_shared_converter_config_alone():
    from telegramify_markdown.config import get_runtime_config

    before = get_runtime_config().markdown_symbol.unordered_list_item
    to_md("- a\n  - b")
    assert get_runtime_config().markdown_symbol.unordered_list_item == before
