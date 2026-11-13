"""The Telegram page render keeps the reference bot's MarkdownV2 output: the escape and
expandable-block golden values (formerly `mdv2_escape` / `expandable` cases of the
render goldens) now come out of a page's summary line and a later page's block."""

from __future__ import annotations

import pytest

from tgforge.base import ui
from tgforge.base.telegram import _page, pages_md

# (text, its MarkdownV2 escape) — the reference goldens
ESCAPES = [
    ("a_b*c[d](e)", "a\\_b\\*c\\[d\\]\\(e\\)"),
    ("version 1.2.3!", "version 1\\.2\\.3\\!"),
    ("back\\slash and #hash", "back\\\\slash and \\#hash"),
]
# (head, body, the expandable block) — the reference goldens
EXPANDABLES = [
    ("Header only", "", "**>Header only||"),
    ("Header", "line1\nline2\nline3", "**>Header\n>\n>line1\n>line2\n>line3||"),
]


@pytest.mark.parametrize("text, escaped", ESCAPES, ids=["markup", "dots", "backslash"])
def test_a_summary_line_is_escaped(text, escaped):
    assert pages_md(ui.Pages(text, [""])) == [(escaped, text)]


@pytest.mark.parametrize("text, escaped", ESCAPES, ids=["markup", "dots", "backslash"])
def test_a_later_page_head_is_escaped(text, escaped):
    assert _page(text, "", False) == (f"**>{escaped}||", text)


@pytest.mark.parametrize("head, body, block", EXPANDABLES, ids=["head-only", "body"])
def test_a_later_page_is_one_expandable_block(head, body, block):
    md, plain = _page(head, body, False)
    assert md == block
    assert plain == head + (f"\n\n{body}" if body else "")
