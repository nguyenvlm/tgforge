"""Unit tests for base render behavior that intentionally diverges from the
reference (so it is not in the differential goldens)."""

from __future__ import annotations

import pytest

from tgforge.base.ui import MAX_MSG, fmt_duration, md_chunks, to_md


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
