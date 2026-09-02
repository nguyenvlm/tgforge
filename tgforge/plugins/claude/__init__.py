"""Claude Code integration for tgforge: the `/claude` agent window class, the
mirror, and the `!` / `!!` one-shot prefixes. Included on an app via
`app.include(Claude(...))`.
"""

from __future__ import annotations

from tgforge.plugins.claude.driver import Claude, ClaudeTopic

__all__ = ["Claude", "ClaudeTopic"]
