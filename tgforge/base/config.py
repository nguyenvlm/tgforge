"""Base per-bot config from a `bot.json`. `home` holds the bot's own state (the
SQLite store) and is the process's working directory. The flat fields configure
Telegram (`token` optional); the optional `slack` block configures Slack; at least one
platform is set. Identity learned at runtime — Telegram's bound `chat_id` and
`bot_username`, Slack's `control_channel` — is written back here, next to each
platform's `owner_id`; secrets and identity never enter the state DB. A workspace
(where an agent runs a turn) is a consumer concept, not a base one — see the claude
plugin.
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, PrivateAttr


class SlackConfig(BaseModel):
    bot_token: str  # xoxb-…: the Web API calls
    app_token: str  # xapp-…: the Socket Mode connection
    owner_id: str | None = None
    control_channel: str | None = None  # the Slack General, created on first start


class BotConfig(BaseModel):
    token: str | None = None
    owner_id: int | None = None
    chat_id: int | None = None
    bot_username: str = ""
    # user-service name — systemd unit / launchd label (enables /restart); None disables it
    service: str | None = None
    # bot state dir + process cwd
    home: str = "."
    slack: SlackConfig | None = None

    _source: Path | None = PrivateAttr(default=None)

    @classmethod
    def load(cls, path: str | Path) -> BotConfig:
        cfg = cls(**json.loads(Path(path).read_text()))
        if not cfg.token and cfg.slack is None:
            raise ValueError(f"{path}: set `token` (Telegram), a `slack` block, or both")
        cfg._source = Path(path)
        return cfg

    @property
    def home_path(self) -> Path:
        return Path(self.home).expanduser().resolve()

    @property
    def db_file(self) -> Path:
        return self.home_path / "state.db"

    def save(self) -> None:
        """Persist identity back to the source bot.json, preserving other keys and
        the token. A config with no source path (tests) is a no-op."""
        if self._source is None:
            return
        data = {}
        if self._source.exists():
            try:
                data = json.loads(self._source.read_text())
            except json.JSONDecodeError:
                data = {}
        data["owner_id"] = self.owner_id
        data["chat_id"] = self.chat_id
        data["bot_username"] = self.bot_username
        if self.slack is not None:
            slack = data.setdefault("slack", {})
            slack["owner_id"] = self.slack.owner_id
            slack["control_channel"] = self.slack.control_channel
        self._source.parent.mkdir(parents=True, exist_ok=True)
        self._source.write_text(json.dumps(data, indent=2))
        self._source.chmod(0o600)  # holds the tokens — keep it owner-only
