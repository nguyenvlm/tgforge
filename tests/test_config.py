import json
from pathlib import Path

from tgforge.base.config import BotConfig


def test_load_reads_json(tmp_path: Path):
    cfg = tmp_path / "bot.json"
    cfg.write_text(json.dumps({"token": "abc", "owner_id": 42}))
    loaded = BotConfig.load(cfg)
    assert loaded.token == "abc"
    assert loaded.owner_id == 42


def test_defaults_when_optional_absent(tmp_path: Path):
    cfg = tmp_path / "bot.json"
    cfg.write_text(json.dumps({"token": "abc"}))
    loaded = BotConfig.load(cfg)
    assert loaded.owner_id is None
    assert loaded.service is None
    assert loaded.home == "."
