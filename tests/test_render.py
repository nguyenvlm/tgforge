"""Unit tests for base render behavior that intentionally diverges from the
reference (so it is not in the differential goldens)."""

from __future__ import annotations

import pytest

from tgforge.base.ui import fmt_duration


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
    from tgforge.plugins.agent.render import status_head

    assert "1h" in status_head(0, 0, 3600, 0)
