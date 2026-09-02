"""The CLI turns Ctrl-C / EOF into a clean exit, not a traceback."""

from __future__ import annotations

import pytest

from tgforge import cli


@pytest.mark.parametrize("exc", [KeyboardInterrupt, EOFError])
def test_interrupt_exits_cleanly(monkeypatch, exc):
    def boom():
        raise exc

    monkeypatch.setattr("tgforge.install.main", boom)
    with pytest.raises(SystemExit) as ei:
        cli.main(["install"])
    assert "aborted" in str(ei.value)  # a message, not a traceback
