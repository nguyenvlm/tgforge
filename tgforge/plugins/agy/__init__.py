"""agy (Antigravity CLI) integration for tgforge: the `/agy` agent window class over
`agy acp`. Included on an app via `app.include(Agy(...))`.
"""

from __future__ import annotations

from tgforge.plugins.agy.driver import Agy, AgyTopic

__all__ = ["Agy", "AgyTopic"]
