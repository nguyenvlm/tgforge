"""The agy window's quota part: `agy --output-format json -p=/usage` polled per
account after a turn, at most once a minute, and the group matching the model."""

from __future__ import annotations

import asyncio
import copy
import logging

from agy_fakes import ACP, SESSION, USAGE, client, session_db, settle, window
from claude_fakes import THREAD, texts

from tgforge.plugins.agent.render import quota_part
from tgforge.plugins.agy import backend, driver

GROUPS = backend.usage_groups(USAGE)


def _user(c, text):
    mid = c.bot._id()
    c.bot.created.append((mid, int(THREAD), text))
    return mid


def _usage(gemini_5h=None, gemini_weekly=None):
    """The captured `/usage` payload with the Gemini group's remaining fractions set."""
    payload = copy.deepcopy(USAGE)
    group = payload["command"]["data"]["groups"][0]
    for bucket in group["buckets"]:
        if bucket["window"] == "5h" and gemini_5h is not None:
            bucket["remaining_fraction"] = gemini_5h
        if bucket["window"] == "weekly" and gemini_weekly is not None:
            bucket["remaining_fraction"] = gemini_weekly
    return payload


# ── the quota part ─────────────────────────────────────────────────


def test_the_group_naming_the_model_gives_its_used_percent():
    assert [g["name"] for g in GROUPS] == [
        "Gemini Models",
        "Claude and GPT models",
        "Gemini 4 Argon",
    ]
    flash = backend.model_quota(GROUPS, "Gemini 3.8 Flash")
    assert flash["status"] == "allowed"
    assert round(flash["five_hour"], 4) == round(1 - 0.9683157205581665, 4)
    assert quota_part(flash) == "5h 3% · 7d 2%"
    assert quota_part(backend.model_quota(GROUPS, "Claude Opus 5.5")) == "5h 0% · 7d 0%"
    # Argon's group lists no models in its description; it is named after the model
    assert quota_part(backend.model_quota(GROUPS, "Gemini 4 Argon")) == "7d 0%"
    # a display name carrying the effort label matches the group's plain listing
    assert quota_part(backend.model_quota(GROUPS, "GPT-OSS 120B (Medium)")) == "5h 0% · 7d 0%"


def test_a_model_no_group_names_shows_no_quota():
    assert backend.model_quota(GROUPS, "Gemini 3") is None  # a name is matched whole
    assert backend.model_quota(GROUPS, "Orchestrator (Argon)") is None  # no group lists it
    assert backend.model_quota(GROUPS, "Gemini 4") is None  # a name is matched whole
    assert backend.model_quota(GROUPS, None) is None
    assert backend.usage_groups({"status": "SUCCESS"}) == []


def test_an_empty_bucket_leads_with_a_warning_and_a_weekly_only_group_shows_7d():
    spent = backend.model_quota(backend.usage_groups(_usage(gemini_5h=0)), "Gemini 4 Neon")
    assert quota_part(spent) == "⚠️ 5h 100% · 7d 2%"
    weekly_only = [
        {
            "description": "Models within this group: Gemini 4 Argon",
            "buckets": [{"window": "weekly", "remaining_fraction": 0.5}],
        }
    ]
    assert quota_part(backend.model_quota(weekly_only, "Gemini 4 Argon")) == "7d 50%"


async def _one_turn(c, t, text="hi"):
    await t.submit(text, reply_to=_user(c, text))
    agent = t.agents[-1]
    agent.send({**ACP["prompt_reply_end_turn"], "id": agent.prompt_ids()[-1]})
    await settle(c, t)


def test_a_turn_end_polls_the_accounts_usage_and_the_status_line_shows_it(tmp_path):
    """The session's model (`gemini-4-neon`, display name "Gemini 4 Neon") is in the
    Gemini group: after the poll the window's status parts carry its quota."""

    async def scenario():
        c = client(tmp_path)
        t = window(c, tmp_path)
        assert t._usage_parts(0) == []
        await _one_turn(c, t)
        assert len(t.usage_polls) == 1
        poll = t.usage_polls[0]
        assert poll.args == [t.plugin.agy_bin, "--output-format", "json", "-p=/usage"]
        assert poll.env["HOME"] == str(__import__("pathlib").Path.home())  # default account
        assert t._usage_parts(0) == ["5h 3% · 7d 2%"]
        assert "5h 3% · 7d 2%" in t._summary(t.turn, t.turn.started)
        await t.on_close()

    asyncio.run(scenario())


def test_a_non_default_account_polls_as_its_home(tmp_path):
    async def scenario():
        c = client(tmp_path)
        t = window(c, tmp_path)
        t.config_dir = str(tmp_path / ".agy-work")
        await _one_turn(c, t)
        assert t.usage_polls[0].env["HOME"] == str(tmp_path / ".agy-work")
        await t.on_close()

    asyncio.run(scenario())


def test_the_poll_runs_at_most_once_a_minute_per_account(tmp_path, monkeypatch):
    async def scenario():
        c = client(tmp_path)
        t = window(c, tmp_path)
        await _one_turn(c, t)
        await _one_turn(c, t, "again")
        assert len(t.usage_polls) == 1  # the second turn ended within the minute
        other = window(c, tmp_path, thread="556")
        other.config_dir = str(tmp_path / ".agy-work")
        await _one_turn(c, other)
        assert len(other.usage_polls) == 1  # another account polls on its own
        monkeypatch.setattr(driver, "QUOTA_POLL_INTERVAL", 0.0)
        await _one_turn(c, t, "later")
        assert len(t.usage_polls) == 3  # its spawn and its end each poll once
        await t.on_close()
        await other.on_close()

    asyncio.run(scenario())


def test_a_failed_poll_keeps_the_last_quota_and_raises_one_notice(tmp_path, monkeypatch):
    monkeypatch.setattr(driver, "QUOTA_POLL_INTERVAL", 0.0)
    notices = []

    async def scenario():
        c = client(tmp_path)
        t = window(c, tmp_path)
        t.plugin.notice = notices.append
        await _one_turn(c, t)
        assert t._usage_parts(0) == ["5h 3% · 7d 2%"]
        t.usage_returncode = 1
        await _one_turn(c, t, "two")
        t.usage_returncode, t.usage_payload = 0, b"not json"
        await _one_turn(c, t, "three")
        assert t._usage_parts(0) == ["5h 3% · 7d 2%"]  # the last value kept
        assert len(notices) == 1 and notices[0].window is None  # in General, once
        assert "agy quota poll" in notices[0].what
        t.usage_payload = _usage(gemini_5h=0.5)
        await _one_turn(c, t, "four")
        assert t._usage_parts(0) == ["5h 50% · 7d 2%"]
        t.usage_returncode = 1
        await _one_turn(c, t, "five")
        assert len(notices) == 2  # failing again after a success raises a new one
        await t.on_close()

    asyncio.run(scenario())


def test_a_model_no_group_names_shows_no_quota_part_in_the_window(tmp_path):
    async def scenario():
        c = client(tmp_path)
        t = window(c, tmp_path)
        t.model = "gemini-pro-orchestrator-g1-pro"
        await _one_turn(c, t)
        assert t.plugin.model_names["gemini-pro-orchestrator-g1-pro"] == "Orchestrator (Argon)"
        assert t._usage_parts(0) == []
        await t.on_close()

    asyncio.run(scenario())


def test_a_running_poll_is_never_joined_by_a_second(tmp_path, monkeypatch):
    monkeypatch.setattr(driver, "QUOTA_POLL_INTERVAL", 0.0)

    async def scenario():
        c = client(tmp_path)
        t = window(c, tmp_path)
        t.usage_hang = asyncio.Event()
        await _one_turn(c, t)
        await _one_turn(c, t, "two")
        assert len(t.usage_polls) == 1  # the first is still running
        t.usage_hang.set()
        await settle(c, t)
        await _one_turn(c, t, "three")
        assert len(t.usage_polls) == 3  # its spawn and its end each poll once
        await t.on_close()

    asyncio.run(scenario())


def test_a_hung_poll_is_stopped_and_counted_as_failed(tmp_path, monkeypatch):
    monkeypatch.setattr(driver, "QUOTA_POLL_TIMEOUT", 0.05)
    notices = []

    async def scenario():
        c = client(tmp_path)
        t = window(c, tmp_path)
        t.plugin.notice = notices.append
        t.usage_hang = asyncio.Event()  # never set: the run never answers
        await _one_turn(c, t)
        await asyncio.sleep(0.15)
        poll = t.usage_polls[0]
        assert poll.returncode == -9  # reaped
        assert len(notices) == 1 and "no answer in 0s" in notices[0].error
        assert t._usage_parts(0) == []
        await t.on_close()

    asyncio.run(scenario())


# ── a poll when the process starts ─────────────────────────────────


def test_the_first_turns_summary_carries_the_quota_a_spawn_poll_brought(tmp_path):
    async def scenario():
        c = client(tmp_path)
        t = window(c, tmp_path)
        await t.submit("hi", reply_to=_user(c, "hi"))
        await settle(c, t)
        assert len(t.usage_polls) == 1 and t.turn_open  # polled at the spawn, mid-turn
        agent = t.agents[-1]
        agent.send({**ACP["prompt_reply_end_turn"], "id": agent.prompt_ids()[-1]})
        await settle(c, t)
        summary = next(x for x in texts(c) if " · ↓ " in x.splitlines()[0])
        assert summary.splitlines()[0].endswith(" · 5h 3% · 7d 2%")
        assert len(t.usage_polls) == 1  # the turn end within the minute starts none
        await t.on_close()

    asyncio.run(scenario())


def test_a_spawn_within_the_interval_starts_no_poll(tmp_path):
    async def scenario():
        c = client(tmp_path)
        first = window(c, tmp_path)
        await first.submit("hi", reply_to=None)
        await settle(c, first)
        second = window(c, tmp_path, thread="556")  # the same account
        await second.submit("hi", reply_to=None)
        await settle(c, second)
        assert len(first.usage_polls) == 1 and second.usage_polls == []
        await first.on_close()
        await second.on_close()

    asyncio.run(scenario())


def test_a_revives_session_load_spawn_polls_too(tmp_path):
    async def scenario():
        c = client(tmp_path)
        t = window(c, tmp_path)
        t.session_id = SESSION
        session_db(tmp_path)
        await t.submit("again", reply_to=None)
        await settle(c, t)
        assert t.agents[-1].requests("session/load") and len(t.usage_polls) == 1
        await t.on_close()

    asyncio.run(scenario())


def test_a_successful_poll_is_logged_with_its_account_and_group_count(tmp_path, caplog):
    async def scenario():
        c = client(tmp_path)
        t = window(c, tmp_path)
        await t.submit("hi", reply_to=None)
        await settle(c, t)
        await t.on_close()
        return t._account_key()

    with caplog.at_level(logging.INFO, logger="tgforge"):
        key = asyncio.run(scenario())
    assert f"agy quota poll ({key}): 3 group(s)" in caplog.messages
