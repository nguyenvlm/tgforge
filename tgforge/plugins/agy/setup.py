"""Machine-side setup for the agy plugin: an `agy-as` helper to run the CLI under a
named account. Idempotent; run at bot startup."""

from __future__ import annotations

import logging
from pathlib import Path

from tgforge.base.kernel import notice_exception

LOGGER = logging.getLogger("tgforge")

_AGY_AS = (
    "#!/bin/sh\n"
    "# installed by tgforge — run agy under a named account\n"
    'name="$1"; shift\n'
    'exec env HOME="$HOME/.agy-$name" agy "$@"\n'
)


def install_agy_as(on_notice=None) -> None:
    """Install ~/.local/bin/agy-as: `agy-as <name> [args]` runs the CLI with the
    account's home `~/.agy-<name>` as `HOME` (print and interactive agy take no data-dir
    flag). A failure is logged and, given `on_notice` (the bot's), raised as an
    exception notice."""
    dest = Path.home() / ".local" / "bin" / "agy-as"
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not dest.exists() or dest.read_text() != _AGY_AS:
            dest.write_text(_AGY_AS)
            dest.chmod(0o755)
            LOGGER.info("installed %s", dest)
    except OSError as e:
        LOGGER.warning("agy-as install failed: %r", e)
        if on_notice is not None:
            notice_exception(on_notice, None, "agy-as install", e)
