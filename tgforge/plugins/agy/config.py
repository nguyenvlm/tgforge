"""agy-app configuration: where the agy CLI and its account data dirs live, and the
model list offered at /agy. Provider-specific — never in base."""

from __future__ import annotations

import shutil
from pathlib import Path

# No model ids baked into the library: with none set (`Agy(models=[...])` or the /model
# UI), the picker offers the models the agent itself lists for the session.
DEFAULT_MODELS: list[list[str]] = []
ACCOUNT_PREFIX = ".agy-"  # an account's home: `~/.agy-<name>`
APP_DATA_DIR = "antigravity-cli"  # agy's data dir name under the Gemini dir
TOKEN_FILE = "antigravity-oauth-token"  # its presence is a signed-in data dir


def agy_bin() -> str:
    return shutil.which("agy") or str(Path.home() / ".local" / "bin" / "agy")


def accounts_root() -> Path:
    """The dir holding the account homes (`.agy-<name>`) and the default account's
    own `.gemini`: the user's home."""
    return Path.home()


def gemini_dir(account_home: Path) -> Path:
    """An account's Gemini dir: `<home>/.gemini`, the dir `agy acp --gemini-dir` takes."""
    return account_home / ".gemini"


def data_dir(account_home: Path) -> Path:
    """An account's agy data dir: sessions (`conversations/`, `brain/`) and its token."""
    return gemini_dir(account_home) / APP_DATA_DIR
