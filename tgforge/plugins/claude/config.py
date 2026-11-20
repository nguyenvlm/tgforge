"""Claude-app configuration: the model list offered at /claude, and where the Claude
CLI + its account config dirs live. Provider-specific — never in base."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

# No model ids baked into the library — a specific list goes stale and is one
# opinion imposed on every bot. A bot sets its own list: `Claude(models=[...])`
# in the app module, or at runtime via the /model UI (persisted per window).
# With none, the picker still offers "Default" — the Claude CLI's own default,
# no --model.
DEFAULT_MODELS: list[list[str]] = []


def claude_dir() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude")).expanduser()


def claude_bin() -> str:
    return shutil.which("claude") or str(Path.home() / ".local" / "bin" / "claude")
