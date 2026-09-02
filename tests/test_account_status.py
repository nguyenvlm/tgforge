"""account_status classifies each account dir's .credentials.json: healthy, access
token expired (refresh still valid), refresh token expired, missing, or unreadable —
and flags which ones need a re-login."""

from __future__ import annotations

import json
import time

from tgforge.plugins.claude import Claude


def _write_creds(dir_path, *, expires_at, refresh_expires_at, sub="pro"):
    dir_path.mkdir(parents=True, exist_ok=True)
    (dir_path / ".credentials.json").write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "subscriptionType": sub,
                    "expiresAt": expires_at,
                    "refreshTokenExpiresAt": refresh_expires_at,
                }
            }
        )
    )


def _plugin(tmp_path):
    p = Claude()
    p.claude_dir = tmp_path / ".claude"  # siblings scanned from tmp_path
    p.accounts = {}
    return p


def _row(rows, name):
    return next(r for r in rows if r["name"] == name)


def test_account_status_covers_every_state(tmp_path):
    now = int(time.time() * 1000)
    day = 86_400_000
    _write_creds(
        tmp_path / ".claude-ok",
        expires_at=now + 5 * day,
        refresh_expires_at=now + 30 * day,
    )
    _write_creds(
        tmp_path / ".claude-stale",
        expires_at=now - day,
        refresh_expires_at=now + 30 * day,
    )
    _write_creds(
        tmp_path / ".claude-dead",
        expires_at=now - 2 * day,
        refresh_expires_at=now - day,
    )
    (tmp_path / ".claude-empty").mkdir()  # named account, but no .credentials.json
    (tmp_path / ".claude-bad").mkdir()
    (tmp_path / ".claude-bad" / ".credentials.json").write_text("{not json")

    plugin = _plugin(tmp_path)
    plugin.accounts = {"empty": str(tmp_path / ".claude-empty")}  # a known account, creds gone
    rows = plugin.account_status()

    assert "OK" in _row(rows, "ok")["label"] and _row(rows, "ok")["expired"] is False
    assert "access token expired" in _row(rows, "stale")["label"]
    assert _row(rows, "stale")["expired"] is False  # refresh still valid → usable
    assert "refresh token expired" in _row(rows, "dead")["label"]
    assert _row(rows, "dead")["expired"] is True
    assert "no credentials" in _row(rows, "empty")["label"]
    assert _row(rows, "empty")["expired"] is True
    assert "error reading creds" in _row(rows, "bad")["label"]
    assert _row(rows, "bad")["expired"] is True


def test_default_account_included_when_absent(tmp_path):
    rows = _plugin(tmp_path).account_status()
    assert any(r["name"] == "default" for r in rows)  # always listed


def test_default_dir_listed_once(tmp_path):
    now = int(time.time() * 1000)
    day = 86_400_000
    # the default dir has creds, so scan_accounts would also name it "claude"
    _write_creds(tmp_path / ".claude", expires_at=now + day, refresh_expires_at=now + 30 * day)
    rows = _plugin(tmp_path).account_status()
    default_dir = str(tmp_path / ".claude")
    assert [r["dir"] for r in rows].count(default_dir) == 1  # one row, not two
    assert sum(1 for r in rows if r["name"] == "default") == 1
