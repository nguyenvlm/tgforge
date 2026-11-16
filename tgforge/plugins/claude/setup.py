"""Machine-side setup for the Claude plugin: an interactive-shell `claude`
wrapper that loads a per-workspace system_prompt.txt, and a `claude-as` helper
to run the CLI under a named account. Idempotent; run at bot startup + install.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from tgforge.base.kernel import notice_exception

LOGGER = logging.getLogger("tgforge")

_WRAPPER_START = "# >>> tgforge claude system-prompt >>>"
_WRAPPER_END = "# <<< tgforge claude system-prompt <<<"
_SHELL_WRAPPER = (
    _WRAPPER_START + "\n"
    "# Loads a workspace system_prompt.txt for interactive `claude` when the\n"
    "# current dir sits inside a workspace that has one. Managed by tgforge.\n"
    "claude() {\n"
    '  local dir="$PWD"\n'
    "  local -a _sp; _sp=()\n"
    '  while [ -n "$dir" ]; do\n'
    '    if [ -f "$dir/system_prompt.txt" ]; then\n'
    '      _sp=(--system-prompt-file "$dir/system_prompt.txt")\n'
    "      break\n"
    "    fi\n"
    '    [ "$dir" = "/" ] && break\n'
    '    dir="${dir%/*}"\n'
    '    [ -z "$dir" ] && dir="/"\n'
    "  done\n"
    "  if [ ${#_sp[@]} -gt 0 ]; then\n"
    '    command claude "${_sp[@]}" "$@"\n'
    "  else\n"
    '    command claude "$@"\n'
    "  fi\n"
    "}\n" + _WRAPPER_END
)

_CLAUDE_AS = (
    "#!/bin/sh\n"
    "# installed by tgforge — run claude under a named account\n"
    'name="$1"; shift\n'
    'exec env CLAUDE_CONFIG_DIR="$HOME/.claude-$name" claude "$@"\n'
)


def install_claude_as(on_notice=None) -> None:
    """Install ~/.local/bin/claude-as: `claude-as <name> [args]` runs the CLI
    under the '.claude-<name>' config dir. A failure is logged and, given `on_notice`
    (the bot's), raised as an exception notice."""
    dest = Path.home() / ".local" / "bin" / "claude-as"
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not dest.exists() or dest.read_text() != _CLAUDE_AS:
            dest.write_text(_CLAUDE_AS)
            dest.chmod(0o755)
            LOGGER.info("installed %s", dest)
    except OSError as e:
        LOGGER.warning("claude-as install failed: %r", e)
        if on_notice is not None:
            notice_exception(on_notice, None, "claude-as install", e)


def install_shell_wrapper(on_notice=None) -> None:
    """Install/refresh the interactive `claude` wrapper in ~/.bashrc + ~/.zshrc. A
    failure is logged and, given `on_notice` (the bot's), raised as an exception
    notice."""
    pattern = re.compile(re.escape(_WRAPPER_START) + r".*?" + re.escape(_WRAPPER_END), re.DOTALL)
    for rc in (Path.home() / ".bashrc", Path.home() / ".zshrc"):
        text = rc.read_text() if rc.exists() else ""
        if pattern.search(text):
            new = pattern.sub(lambda _: _SHELL_WRAPPER, text)
        elif text.strip():
            new = text.rstrip("\n") + "\n\n" + _SHELL_WRAPPER + "\n"
        else:
            new = _SHELL_WRAPPER + "\n"
        if new != text:
            try:
                rc.write_text(new)
                LOGGER.info("claude system-prompt wrapper written to %s", rc)
            except OSError as e:
                LOGGER.warning("shell wrapper install failed for %s: %r", rc, e)
                if on_notice is not None:
                    notice_exception(on_notice, None, f"shell wrapper install ({rc.name})", e)
