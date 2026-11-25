"""agy window rules around jobs and commands: a running job keeps its process (no
setting or timer ends it; fresh-session switches refuse while it runs), a model / effort
reset reaches the session, `/stop` / `/status` / `/mode` / `/workspace` / skills /
`/rename` / the mirror / `on_restarted` in a window, the transcript drain before a wake
turn, `/kill`'s mid-turn note order and its process-tree kill, the reply's token count,
the `skip` and `/effort` switches, `/clear`, the model pool, and both agent plugins
sharing `!` / `/login`."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from agy_fakes import (
    ACP,
    AGY_PID,
    ARGV,
    JOB_COMMAND,
    SESSION,
    TABLE,
    client,
    last_message,
    message,
    session_db,
    settle,
    step,
    text_chunk,
    window,
    write_steps,
)
from claude_fakes import THREAD, screen, texts

from tgforge.base.kernel import (
    matching_descendants,
    process_tree,
)
from tgforge.plugins.agent import background as jobs
from tgforge.plugins.agent import window as agent_window
from tgforge.plugins.agent.window import AgentTopic
from tgforge.plugins.agy import Agy, driver
from tgforge.plugins.claude import Claude
from tgforge.testing import TestClient


def _user(c, text):
    mid = c.bot._id()
    c.bot.created.append((mid, int(THREAD), text))
    return mid


def _reply(agent, name="prompt_reply_end_turn", **result):
    msg = {**ACP[name], "id": agent.prompt_ids()[-1]}
    if result:
        msg["result"] = {**msg["result"], **result}
    agent.send(msg)


def _captured_processes(monkeypatch):
    """The window's process probes read the captured table (`agy acp` → `exe` → `bash
    -c <CommandLine>` → `sleep`); returns the list each kill records its tree into."""
    killed = []

    async def kill(tree, grace=2.0):
        killed.append(set(tree))
        return len(tree)

    def processes(root, cmd):  # the window's agy is the captured `agy acp`
        assert root == 0  # the fake agent's pid
        return matching_descendants(TABLE, ARGV.get, AGY_PID, cmd)

    monkeypatch.setattr(driver, "command_processes", processes)
    monkeypatch.setattr(driver, "process_tree", lambda roots: process_tree(roots, TABLE))
    monkeypatch.setattr(driver, "kill_processes", kill)
    return killed


async def _turn_with_captured_job(c, t):
    """A prompted turn ending with the captured job still running (its agy is the
    captured `agy acp`)."""
    await t.submit("start it", reply_to=_user(c, "start it"))
    agent = t.agents[-1]
    raw = {"CommandLine": JOB_COMMAND, "WaitMsBeforeAsync": 500, "toolSummary": "bg job"}
    agent.send(message("job_tool_call", rawInput=raw, args=raw, title=JOB_COMMAND))
    _reply(agent)
    await settle(c, t)
    job_id = next(iter(t.background_tasks))
    assert t.background_tasks[job_id]["done"] is None and agent.returncode is None
    return agent, job_id


# ── jobs end with their process ─────────────────────────────────


def test_clear_with_a_running_job_is_refused(tmp_path, monkeypatch):
    """`/clear` would end the process and with it the user's job: refused, naming the
    job; the session, the process and the job row stay."""
    killed = _captured_processes(monkeypatch)

    async def scenario():
        c = client(tmp_path)
        t = window(c, tmp_path)
        agent, job_id = await _turn_with_captured_job(c, t)
        await t.clear(None)
        await settle(c, t)
        assert agent.returncode is None and t.session_id == SESSION
        assert t.background_tasks[job_id]["done"] is None and killed == []
        assert any(
            text.startswith("a background job runs (bg job) — /kill it or wait for it")
            for text in texts(c)
        )
        await t.on_close()

    asyncio.run(scenario())


def test_the_idle_release_waits_for_the_last_job_to_end(tmp_path, monkeypatch):
    """The idle release never ends a process running a job: it waits again, and ends the
    process once the job is over and the idle time has passed."""
    killed = _captured_processes(monkeypatch)
    monkeypatch.setattr(agent_window, "RELEASE_IDLE_SEC", 0.2)

    async def scenario():
        c = client(tmp_path)
        t = window(c, tmp_path)
        agent, job_id = await _turn_with_captured_job(c, t)
        await asyncio.sleep(0.5)  # two idle periods
        assert agent.returncode is None and t.release_task is not None
        assert t.background_tasks[job_id]["done"] is None and killed == []
        jobs.end_job(t, job_id, "✓")  # the job is over (no new ACP line)
        await asyncio.sleep(0.3)
        await settle(c, t)
        assert agent.returncode == -9 and killed == []
        await t.on_close()

    asyncio.run(scenario())


def test_a_crashed_process_ends_its_jobs_rows(tmp_path, monkeypatch):
    killed = _captured_processes(monkeypatch)

    async def scenario():
        c = client(tmp_path)
        t = window(c, tmp_path)
        agent, job_id = await _turn_with_captured_job(c, t)
        agent.exit()  # its output ends: the process is gone
        await settle(c, t)
        assert t.background_tasks[job_id]["done"] == "◼"
        assert killed == []  # the window did not end it: nothing of its own to kill
        t._paint_panel(0)
        await settle(c, t)
        assert any("◼ bg job" in text and "ended" in text for text in texts(c))  # its row
        await t.on_close()

    asyncio.run(scenario())


def test_closing_the_window_closes_the_session_and_kills_no_job_tree(tmp_path, monkeypatch):
    """The window's close ends the process (agy takes its jobs down itself, measured);
    the window kills no tree of its own, and the job row ends as ◼."""
    killed = _captured_processes(monkeypatch)

    async def scenario():
        c = client(tmp_path)
        t = window(c, tmp_path)
        agent, job_id = await _turn_with_captured_job(c, t)
        await t.on_close()
        assert killed == []
        assert agent.requests("session/close") and agent.returncode == -9
        await asyncio.sleep(0.05)
        assert t.background_tasks[job_id]["done"] == "◼"

    asyncio.run(scenario())


# ── a model / effort reset reaches the session ──────────────────


def _menu_answers(t, *answers):
    queue = list(answers)

    async def menu(*args, **kwargs):
        return queue.pop(0)

    t.menu = menu


async def _with_job_kept(c, t):
    """An idle window whose process is kept (a job runs), with a model and effort set."""
    t.model, t.effort = "claude-opus-5-5", "xhigh"
    await t.submit("start", reply_to=None)
    agent = t.agents[-1]
    agent.send(message("job_tool_call"))
    _reply(agent)
    await settle(c, t)
    assert agent.returncode is None and not t.busy
    return agent


def test_model_default_sends_the_sessions_default_and_keeps_the_job(tmp_path):
    """`/model` → Default with a job running: one `set_config_option` with the model the
    session started on (`session/new`'s `currentValue`), the process and the job kept."""

    async def scenario():
        c = client(tmp_path)
        t = window(c, tmp_path)
        agent = await _with_job_kept(c, t)
        before = len(agent.requests("session/set_config_option"))
        _menu_answers(t, "__default__")
        await t._switch_model()
        await settle(c, t)
        sent = agent.requests("session/set_config_option")[before:]
        assert [(m["params"]["configId"], m["params"]["value"]) for m in sent] == [
            ("model", "gemini-4-neon")
        ]
        assert t.model is None and agent.returncode is None
        assert next(iter(t.background_tasks.values()))["done"] is None
        await t.on_close()

    asyncio.run(scenario())


def test_effort_default_sends_the_sessions_default_and_keeps_the_job(tmp_path):
    async def scenario():
        c = client(tmp_path)
        t = window(c, tmp_path)
        agent = await _with_job_kept(c, t)
        before = len(agent.requests("session/set_config_option"))
        _menu_answers(t, "__default__")
        await t.effort_cmd(None)
        await settle(c, t)
        sent = agent.requests("session/set_config_option")[before:]
        assert [(m["params"]["configId"], m["params"]["value"]) for m in sent] == [
            ("thought_level", "high")
        ]
        assert t.effort is None and agent.returncode is None
        assert next(iter(t.background_tasks.values()))["done"] is None
        await t.on_close()

    asyncio.run(scenario())


def test_the_session_defaults_are_the_first_replys_and_survive_a_load(tmp_path):
    """The defaults are kept with the session: a later load (its reply carrying the
    values set since) keeps the ones the session started with."""

    async def scenario():
        session_db(tmp_path)
        c = client(tmp_path)
        t = window(c, tmp_path)
        t.saved["agy_defaults"] = {"session": SESSION, "values": {"model": "gemini-3.8-flash"}}
        t.session_id = SESSION
        await t.submit("again")
        assert t.session_defaults == {"model": "gemini-3.8-flash"}
        await t.on_close()

    asyncio.run(scenario())


def test_a_skip_switch_waits_for_the_jobs_end_before_the_respawn(tmp_path):
    """`/mode` → skip with a job running keeps the process (its job with it); the next
    spawn, after the job and its process ended, runs with the flag."""

    async def scenario():
        c = client(tmp_path)
        t = window(c, tmp_path)
        agent = await _with_job_kept(c, t)
        t.plugin.set_permission_mode("skip")
        await t.backend_set_option("mode", "skip")
        await settle(c, t)
        assert agent.returncode is None and len(t.agents) == 1
        assert next(iter(t.background_tasks.values()))["done"] is None
        agent.exit()  # the job, then its process, ended
        await settle(c, t)
        await t.submit("again")
        assert t.agents[-1] is not agent
        assert t.agents[-1].args[-1] == "--dangerously-skip-permissions"
        await t.on_close()

    asyncio.run(scenario())


def test_an_account_switch_with_a_running_job_is_refused(tmp_path):
    async def scenario():
        c = client(tmp_path)
        t = window(c, tmp_path)
        agent, job_id = await _with_job_kept_plain(c, t)
        _menu_answers(t, None)  # were the menu shown, it would be left unanswered
        await t._pick_account()
        await settle(c, t)
        assert agent.returncode is None and t.config_dir is None
        assert t.background_tasks[job_id]["done"] is None
        assert any(text.startswith("a background job runs (") for text in texts(c))
        await t.on_close()

    asyncio.run(scenario())


async def _one_turn(c, t, text="hi"):
    await t.submit(text, reply_to=_user(c, text))
    _reply(t.agents[-1])
    await settle(c, t)


# ── the window-level commands ───────────────────────────────────


def test_stop_closes_the_session_and_waits_for_its_reply_before_the_kill(tmp_path, monkeypatch):
    async def scenario():
        c = client(tmp_path)
        t = window(c, tmp_path)
        agent, _job = await _with_job_kept_plain(c, t)
        alive_at_close = []
        answer = agent.answer

        def record(msg):
            if msg.get("method") == "session/close":
                alive_at_close.append(agent.returncode)
            answer(msg)

        agent.answer = record

        async def ask_buttons(*args, **kwargs):
            return "yes", None

        t.ask_buttons = ask_buttons
        await t.stop(None)
        await settle(c, t)
        assert agent.requests("session/close")[0]["params"] == {"sessionId": SESSION}
        assert alive_at_close == [None] and agent.returncode == -9
        assert t.window not in c.core.instances

    asyncio.run(scenario())


def test_an_unanswered_close_is_waited_on_only_for_its_bound(tmp_path, monkeypatch):
    monkeypatch.setattr(driver, "CLOSE_TIMEOUT", 0.05)

    async def scenario():
        c = client(tmp_path)
        t = window(c, tmp_path)
        agent, _job = await _with_job_kept_plain(c, t)
        agent.answers_close = False
        await asyncio.wait_for(t.on_close(), 1)
        assert agent.requests("session/close") and agent.returncode == -9

    asyncio.run(scenario())


async def _with_job_kept_plain(c, t):
    """An idle window whose process a running job keeps alive."""
    await t.submit("start", reply_to=None)
    agent = t.agents[-1]
    agent.send(message("job_tool_call"))
    _reply(agent)
    await settle(c, t)
    return agent, next(iter(t.background_tasks))


def test_status_names_the_window_state_account_model_and_mode(tmp_path):
    async def scenario():
        c = client(tmp_path)
        t = window(c, tmp_path)
        t.model = "gemini-3.8-flash"
        line = await t.status(None)
        assert line == "work · idle · default (default) · model gemini-3.8-flash · mode auto"

    asyncio.run(scenario())


def test_a_skip_switch_held_by_a_job_says_so_in_the_reply_and_the_status(tmp_path):
    """Leaving `skip` while a job keeps a process started with the flag: the window
    goes on running `skip` until the job ends, and both the `/mode` reply and `/status`
    say so (the app's mode alone would claim the switch took effect)."""

    async def scenario():
        c = client(tmp_path)
        t = window(c, tmp_path)
        t.plugin.set_permission_mode("skip")
        agent, _job = await _with_job_kept_plain(c, t)
        assert agent.args[-1] == "--dangerously-skip-permissions"
        _menu_answers(t, "default")
        await t.mode(None)
        await settle(c, t)
        assert agent.returncode is None
        reply = [x for x in texts(c) if x.startswith("permission mode → default")][-1]
        assert "keeps running skip until its job ends (" in reply
        line = await t.status(None)
        assert line.endswith("mode skip (→ default after its job ends)")
        await t.on_close()

    asyncio.run(scenario())


def test_a_skip_switch_during_a_turn_with_no_job_names_no_job(tmp_path):
    """A busy turn keeps its process, but with no job its end drops the process, so the
    switch applies from the next turn: the reply and `/status` name no held job."""

    async def scenario():
        c = client(tmp_path)
        t = window(c, tmp_path)
        t.plugin.set_permission_mode("skip")
        await t.submit("hi", reply_to=_user(c, "hi"))
        await settle(c, t)
        agent = t.agents[-1]
        _menu_answers(t, "default")
        await t.mode(None)
        await settle(c, t)
        assert [x for x in texts(c) if x.startswith("permission mode →")][-1] == (
            "permission mode → default"
        )
        assert (await t.status(None)).endswith("mode default")
        _reply(agent)
        await settle(c, t)
        await t.on_close()

    asyncio.run(scenario())


def test_stop_with_a_running_job_says_closing_also_stops_it(tmp_path):
    async def scenario():
        c = client(tmp_path)
        t = window(c, tmp_path)
        await _with_job_kept_plain(c, t)
        asked = []

        async def ask_buttons(question, buttons, **kw):
            asked.append(question)
            return None, None

        t.ask_buttons = ask_buttons
        await t.stop(None)
        assert "This also stops the running job (" in asked[0]
        await t.on_close()

    asyncio.run(scenario())


def test_a_workspace_switch_starts_a_new_session_in_a_new_process(tmp_path):
    async def scenario():
        first, second = tmp_path / "a", tmp_path / "b"
        first.mkdir()
        second.mkdir()
        c = client(tmp_path, roots=[str(first), str(second)])
        t = window(c, tmp_path)
        t.workspace = str(first)
        agent, job_id = await _with_job_kept_plain(c, t)
        _menu_answers(t, str(second.resolve()))
        await t._switch_workspace()  # a job runs: refused, nothing changes
        await settle(c, t)
        assert agent.returncode is None and t.workspace == str(first)
        assert t.background_tasks[job_id]["done"] is None
        assert any(text.startswith("a background job runs (") for text in texts(c))
        agent.exit()  # the job ended with its process
        await settle(c, t)
        await t.submit("in a")
        _reply(t.agents[-1])
        await settle(c, t)  # an idle turn end: that process ended
        _menu_answers(t, str(second.resolve()))
        await t._switch_workspace()
        assert t.workspace == str(second.resolve()) and t.session_id is None
        await t.submit("in b")
        newest = t.agents[-1]
        assert newest.requests("session/new")[0]["params"]["cwd"] == str(second.resolve())
        await t.on_close()

    asyncio.run(scenario())


def test_a_slash_command_goes_to_agy_as_the_prompt_text(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_window, "MESSAGE_DEBOUNCE", 0.01)

    async def scenario():
        c = client(tmp_path)
        t = window(c, tmp_path)
        await c.send("/probe-skill some args", window=THREAD)
        await settle(c, t)
        prompt = t.agents[0].requests("session/prompt")[0]["params"]["prompt"]
        assert prompt == [{"type": "text", "text": "/probe-skill some args"}]
        await t.on_close()

    asyncio.run(scenario())


def test_rename_sets_the_title_and_a_bare_rename_takes_agys_title(tmp_path):
    async def scenario():
        c = client(tmp_path)
        t = window(c, tmp_path)
        await t.rename_cmd(SimpleNamespace(args="My window"))
        assert t.title == "My window" and t.saved["base_name"] == "My window"
        await t.submit("hi")
        t.agents[0].send(message("session_info_update", title="Named by agy"))
        await settle(c, t)
        assert t.title == "My window" and t.saved["base_name"] == "My window"  # no rename
        await t.rename_cmd(SimpleNamespace(args=""))
        assert t.title == "Named by agy" and t.saved["base_name"] == "Named by agy"
        await t.on_close()

    asyncio.run(scenario())


def test_the_mirror_shows_a_session_run_on_the_pc(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_window, "MIRROR_INTERVAL", 0.01)

    async def scenario():
        c = client(tmp_path)
        t = window(c, tmp_path)
        t.session_id = SESSION
        write_steps(t)  # the transcript exists: the mirror starts at its end
        t._start_mirror()
        await settle(c, t)
        write_steps(t, step("user_input"), step("planner_final"))
        await settle(c, t, rounds=8)
        shown = texts(c)
        assert any(s.startswith("[user] Start the shell command") for s in shown)
        assert "[cli] started 1" in shown
        await t.on_close()

    asyncio.run(scenario())


def test_on_restarted_resumes_the_session_with_the_restart_prompt(tmp_path):
    async def scenario():
        session_db(tmp_path)
        c = client(tmp_path)
        t = window(c, tmp_path)
        t.session_id = SESSION
        await t.on_restarted()
        agent = t.agents[0]
        assert agent.requests("session/load")
        text = agent.requests("session/prompt")[0]["params"]["prompt"][0]["text"]
        assert text.startswith("🔄 Bot restarted and is back online")
        await t.on_close()

    asyncio.run(scenario())


# ── the transcript drain before a wake turn ─────────────────────


def test_the_earlier_turns_final_step_never_ends_the_wake_turn(tmp_path):
    """The prompted turn's final `PLANNER_RESPONSE` is written after its reply, and the
    job's `completed` arrives before the next transcript poll: the wake turn stays open."""

    async def scenario():
        c = client(tmp_path)
        t = window(c, tmp_path)
        await t.submit("start", reply_to=_user(c, "start"))
        agent = t.agents[0]
        agent.send(message("job_tool_call"))
        _reply(agent)
        await asyncio.sleep(0.01)  # the reply is read; no transcript poll ran yet
        write_steps(t, step("planner_final", content="started"))
        agent.send(message("job_tool_call_completed"))
        await asyncio.sleep(0.6)  # two polls of TRANSCRIPT_POLL
        await settle(c, t)
        assert t.turn_open and t.turn.prompt_id is None
        await t.on_close()

    asyncio.run(scenario())


# ── /kill's mid-turn note stays above the turn ─────────────────


def test_kill_mid_turn_keeps_the_card_below_the_note(tmp_path, monkeypatch):
    _captured_processes(monkeypatch)

    async def scenario():
        c = client(tmp_path)
        t = window(c, tmp_path)
        agent, job_id = await _turn_with_captured_job(c, t)
        await t.submit("next", reply_to=_user(c, "next"))
        await settle(c, t)
        await t._stop_job(job_id)
        await settle(c, t)
        card = t.turn.card_id
        agent.send(text_chunk("agent_message_chunk", "NEXT DONE"))
        _reply(agent)
        await settle(c, t)
        ids = [mid for mid, _text in screen(c)]
        note = next(mid for mid, text in screen(c) if text.startswith("stopping bg job"))
        answer = next(mid for mid, text in screen(c) if text.startswith("NEXT DONE"))
        assert ids.index(note) < ids.index(card) < ids.index(answer)
        assert last_message(c).startswith("NEXT DONE")
        await t.on_close()

    asyncio.run(scenario())


# ── a prompted turn's tokens come from its replies ─────────────


def test_a_prompted_turn_counts_the_tokens_its_reply_reports(tmp_path):
    async def scenario():
        c = client(tmp_path)
        t = window(c, tmp_path)
        await t.submit("hi", reply_to=_user(c, "hi"))
        await settle(c, t)
        write_steps(t, step("planner_tool_calls"))  # 41 tokens read so far
        t._drain_transcript()
        assert t.turn.tok_base + t.turn.tok_latest == 41
        agent = t.agents[0]
        agent.send(text_chunk("agent_message_chunk", "ANSWER"))
        _reply(agent, usage={"outputTokens": 650})
        await settle(c, t)
        summary = next(text for _mid, text in screen(c) if text.startswith("💬") or "↓" in text)
        assert "↓ 650" in summary
        await t.on_close()

    asyncio.run(scenario())


def test_kill_stops_the_jobs_process_tree_and_refuses_a_double_match(tmp_path, monkeypatch):
    killed = []

    async def fake_kill(tree, grace=2.0):
        killed.append(tree)
        return len(tree)

    monkeypatch.setattr(driver, "kill_processes", fake_kill)
    monkeypatch.setattr(driver, "process_tree", lambda roots: roots | {pid + 1 for pid in roots})

    async def scenario():
        c = client(tmp_path)
        t = window(c, tmp_path)
        await t.submit("start", reply_to=None)
        agent = t.agents[0]
        agent.pid = 4242
        agent.send(message("job_tool_call"))
        agent.send({**ACP["prompt_reply_end_turn"], "id": agent.prompt_ids()[0]})
        await settle(c, t)
        job_id = next(iter(t.background_tasks))
        command = ACP["job_tool_call"]["params"]["update"]["rawInput"]["CommandLine"]
        seen = []

        def matches(root, cmd):
            seen.append((root, cmd))
            return found

        monkeypatch.setattr(driver, "command_processes", matches)
        found = [7, 8]
        await t._stop_job(job_id)
        await settle(c, t)
        assert killed == [] and any("refused to stop" in text for text in texts(c))
        found = [7]
        await t._stop_job(job_id)
        await settle(c, t)
        assert seen[-1] == (4242, command)
        assert killed == [{7, 8}]
        assert t.background_tasks[job_id]["stopping"]
        assert any(text.startswith("stopping Start background sleep") for text in texts(c))
        await t.on_close()

    asyncio.run(scenario())


def test_skip_mode_respawns_with_the_flag_and_sets_no_mode(tmp_path):
    async def scenario():
        c = client(tmp_path)
        t = window(c, tmp_path)
        await t.submit("hi")
        agent = t.agents[0]
        agent.send({**ACP["prompt_reply_end_turn"], "id": agent.prompt_ids()[0]})
        await settle(c, t)
        t.plugin.set_permission_mode("skip")
        await t.backend_set_option("mode", "skip")
        await t.submit("again")
        newest = t.agents[-1]
        assert newest is not agent
        assert newest.args[-1] == "--dangerously-skip-permissions"
        configs = [m["params"]["configId"] for m in newest.requests("session/set_config_option")]
        assert "mode" not in configs
        await t.on_close()

    asyncio.run(scenario())


def test_effort_switch_reaches_a_live_idle_session(tmp_path):
    async def scenario():
        c = client(tmp_path)
        t = window(c, tmp_path)
        await t.submit("start", reply_to=None)
        agent = t.agents[0]
        agent.send(message("job_tool_call"))  # a job keeps the process
        agent.send({**ACP["prompt_reply_end_turn"], "id": agent.prompt_ids()[0]})
        await settle(c, t)
        t.effort = "xhigh"
        await t.backend_set_option("effort", "xhigh")
        last = agent.requests("session/set_config_option")[-1]["params"]
        assert (last["configId"], last["value"]) == ("thought_level", "xhigh")
        assert "xhigh" in t.EFFORTS
        await t.on_close()

    asyncio.run(scenario())


def test_clear_starts_a_new_session_in_a_new_process(tmp_path):
    async def scenario():
        c = client(tmp_path)
        t = window(c, tmp_path)
        await t.submit("hi")
        agent = t.agents[0]
        agent.send({**ACP["prompt_reply_end_turn"], "id": agent.prompt_ids()[0]})
        await settle(c, t)
        session_db(tmp_path)
        await t.clear(None)
        assert t.session_id is None
        await t.submit("fresh")
        assert len(t.agents) == 2
        assert t.agents[1].requests("session/new") and not t.agents[1].requests("session/load")
        await t.on_close()

    asyncio.run(scenario())


def test_the_model_pool_is_the_sessions_model_options_when_the_app_sets_none(tmp_path):
    async def scenario():
        c = client(tmp_path)
        t = window(c, tmp_path)
        assert t.plugin.model_pool() == []
        await t.submit("hi")
        options = next(
            o for o in ACP["session_new_result"]["result"]["configOptions"] if o["id"] == "model"
        )
        assert t.plugin.model_pool() == [[o["name"], o["value"]] for o in options["options"]]
        set_models = client(tmp_path / "b", models=[["Flash", "gemini-3.8-flash"]])
        assert set_models.core.plugin_by_id["agy"].model_pool() == [["Flash", "gemini-3.8-flash"]]
        await t.on_close()

    (tmp_path / "b").mkdir()
    asyncio.run(scenario())


def test_both_agent_plugins_share_the_bang_prefixes_login_and_the_service(tmp_path):
    """Two agent plugins in one app: the inherited `!`, `!!`, `/login` and
    `agent.prompt` register once; `!` runs in either window."""
    c = TestClient(Claude(), Agy(), home=str(tmp_path))
    reg = c.core.registry
    assert reg.prefixes["!"][0] == "claude" and reg.universals["/login"][0] == "claude"
    assert set(reg.launches) >= {"/claude", "/agy"}
    agy = c.core.plugin_by_id["agy"]
    assert agy.peers == tuple(c.core.plugins)
    assert [p.id for p in agy._agent_plugins()] == ["claude", "agy"]

    async def scenario():
        t = window(c, tmp_path)
        assert isinstance(t, AgentTopic)
        await c.send("!echo shared-bang", window=THREAD)
        await settle(c, t)
        assert any("shared-bang" in text for text in texts(c))

    asyncio.run(scenario())


def _close_question(c, t):
    """Run the kernel's `/close` in the window; returns the question it asks."""
    asked = []

    async def ask_buttons(question, buttons, **kw):
        asked.append(question)
        return None, None

    ctx = SimpleNamespace(topic=t, ask_buttons=ask_buttons)
    return asked, c.core._core_plugin.close(ctx)


def test_close_with_a_running_job_says_closing_also_stops_it(tmp_path):
    async def scenario():
        c = client(tmp_path)
        t = window(c, tmp_path)
        asked, run = _close_question(c, t)
        await run
        assert asked == ["Close this window?"]  # no job: no warning
        await _with_job_kept_plain(c, t)
        asked, run = _close_question(c, t)
        await run
        label = next(iter(t.background_tasks.values()))["label"]
        assert asked == [f"Close this window? This also stops the running job ({label})."]
        await t.on_close()

    asyncio.run(scenario())


def test_stops_confirm_reads_as_before(tmp_path):
    async def scenario():
        c = client(tmp_path)
        t = window(c, tmp_path)
        await _with_job_kept_plain(c, t)
        asked = []

        async def ask_buttons(question, buttons, **kw):
            asked.append(question)
            return None, None

        t.ask_buttons = ask_buttons
        await t.stop(None)
        label = next(iter(t.background_tasks.values()))["label"]
        assert asked == [
            "Close topic 'work'? The session stays on disk, resumable with /cli. "
            f"This also stops the running job ({label})."
        ]
        await t.on_close()

    asyncio.run(scenario())


def test_close_in_a_claude_window_carries_no_warning(tmp_path):
    from claude_fakes import window as claude_window

    async def scenario():
        c = TestClient(Claude(), home=str(tmp_path))
        t = claude_window(c, tmp_path)
        asked, run = _close_question(c, t)
        await run
        assert asked == ["Close this window?"]

    asyncio.run(scenario())
