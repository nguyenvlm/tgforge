"""The backend-neutral agent window core: `AgentTopic` (turn lifecycle, send order,
live card, settle, notices, buffer, background panel, the shared commands) and
`AgentPlugin` (model pool, workspace roots, accounts, `/login`, `!` / `!!`). Each agent
CLI is one backend subclass that drives it through the backend boundary.
"""

from __future__ import annotations

from tgforge.plugins.agent.window import AgentPlugin, AgentTopic

__all__ = ["AgentPlugin", "AgentTopic"]
