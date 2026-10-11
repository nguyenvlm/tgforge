"""Claude-app configuration: the model list offered at /claude, the workspace
roots a session can run in, and where the Claude CLI + its account config dirs
live. Provider-specific — never in base."""

from __future__ import annotations

import glob as _glob
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


def system_prompt_file(workspace: Path) -> Path | None:
    """The nearest system_prompt.txt at or above the workspace — the same lookup
    the interactive shell wrapper (setup.py) does, so bot turns and CLI sessions
    run under one system prompt."""
    for directory in (workspace, *workspace.parents):
        candidate = directory / "system_prompt.txt"
        if candidate.is_file():
            return candidate
    return None


# ── Workspace roots ────────────────────────────────────────────────


def resolve_roots(globs: list[str]) -> list[Path]:
    """Expand workspace-root globs/paths to existing dirs; order preserved, deduped."""
    out: list[Path] = []
    seen: set[Path] = set()
    for pattern in globs:
        p = str(Path(pattern).expanduser())
        matches = _glob.glob(p) if any(c in p for c in "*?[") else [p]
        for m in sorted(matches):
            path = Path(m)
            if path.is_dir():
                rp = path.resolve()
                if rp not in seen:
                    seen.add(rp)
                    out.append(rp)
    return out


def _roots_store(home: Path):
    """The claude plugin's namespace in the one state.db — the same store the
    running bot reads, so offline edits (install / cli) and runtime stay in sync."""
    from tgforge.base.kernel import AppDB, plugin_ns

    return AppDB(home / "state.db").saved(plugin_ns("claude"))


def read_roots(home: Path) -> list[str]:
    return list(_roots_store(home).get("roots") or [])


def write_roots(home: Path, roots: list[str]) -> None:
    _roots_store(home)["roots"] = list(roots)
