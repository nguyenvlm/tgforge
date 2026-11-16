"""The Telegram page render's plain text: a page's summary line alone, a summary above
its body, and a later page headed `↳ i/n` a blank line above its body."""

from __future__ import annotations

import pytest

from tgforge.base import ui
from tgforge.base.telegram import _page, pages_plain


def test_a_summary_line_alone_is_its_text():
    assert pages_plain(ui.Pages("a_b*c[d](e)", [""])) == ["a_b*c[d](e)"]


@pytest.mark.parametrize(
    "head, body, plain",
    [("Header only", "", "Header only"), ("Header", "line1\nline2", "Header\n\nline1\nline2")],
    ids=["head-only", "body"],
)
def test_a_later_page_is_its_head_a_blank_line_and_its_body(head, body, plain):
    assert _page(head, body, False) == plain


def test_page_one_is_the_summary_above_its_body_and_the_note_below():
    assert pages_plain(ui.Pages("🔧 1", ["a\nb"], "note")) == ["🔧 1\na\nb\n\nnote"]
