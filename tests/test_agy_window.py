"""The agy window's turns, end to end over a scripted `agy acp` (agy_fakes): the
session start, a prompted turn, the fold signal, cancel, wake turns ending on the
transcript marker, the denied permission ask, revive with the history replay dropped,
a process exit with queued messages, wake-turn tokens and `/cancel`, and image blocks.
"Below" == a higher message id."""

from __future__ import annotations

import asyncio
import base64
import json

from agy_fakes import (
    ACP,
    SESSION,
    FakeAgent,
    client,
    fast_transcript,
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

from tgforge.plugins.agent.window import ATTACHED_HEAD, SESSION_BRIEF, UNDELIVERED_NOTE
from tgforge.plugins.agy.backend import DENIED


def _user(c, text):
    """The user's own message in the thread (inbound, so the bot never sent it)."""
    mid = c.bot._id()
    c.bot.created.append((mid, int(THREAD), text))
    return mid


def _find(c, token):
    return next(mid for mid, text in screen(c) if token in text)


def _reply(agent, prompt_index, name, **result):
    msg = dict(ACP[name])
    msg = {**msg, "id": agent.prompt_ids()[prompt_index]}
    if result:
        msg["result"] = {**msg["result"], **result}
    agent.send(msg)


def test_the_first_prompt_starts_agy_acp_and_a_new_session_with_the_note(tmp_path):
    """One `agy acp` per window: initialize, then session/new in the workspace with the
    session note (after the workspace's system_prompt.txt) appended to the system
    prompt, then the window's options; the session id agy gives is saved."""

    async def scenario():
        (tmp_path / "system_prompt.txt").write_text("WORKSPACE PROMPT")
        c = client(tmp_path)
        t = window(c, tmp_path)
        t.model, t.effort = "gemini-3.8-flash", "low"
        await t.submit("hi", reply_to=_user(c, "hi"))
        agent = t.agents[0]
        assert agent.args == [t.plugin.agy_bin, "acp"]
        methods = [m["method"] for m in agent.received]
        assert methods[:2] == ["initialize", "session/new"]
        new = agent.requests("session/new")[0]["params"]
        assert new["cwd"] == str(tmp_path) and new["mcpServers"] == []
        append = new["_meta"]["antigravity"]["systemPrompt"]["append"]
        assert append.startswith("WORKSPACE PROMPT\n\n[via ")
        assert SESSION_BRIEF[:40] in append
        options = [
            (m["params"]["configId"], m["params"]["value"])
            for m in agent.requests("session/set_config_option")
        ]
        assert options == [
            ("model", "gemini-3.8-flash"),
            ("thought_level", "low"),
            ("mode", "auto"),
        ]
        assert t.session_id == SESSION and t.saved["session"]["session_id"] == SESSION
        prompt = agent.requests("session/prompt")[0]["params"]
        assert prompt == {"sessionId": SESSION, "prompt": [{"type": "text", "text": "hi"}]}
        await t.on_close()

    asyncio.run(scenario())


def test_a_prompted_turn_ends_on_its_prompt_reply(tmp_path):
    """Thinking, a tool call and the answer stream in; the `session/prompt` reply ends
    the turn: the card becomes the summary with the timeline, the answer below it."""

    async def scenario():
        c = client(tmp_path)
        t = window(c, tmp_path)
        await t.submit("run it", reply_to=_user(c, "run it"))
        agent = t.agents[0]
        agent.send(text_chunk("agent_thought_chunk", "THINKING"))
        agent.send(message("tool_call_in_progress"))
        agent.send(message("job_tool_call_completed", toolCallId="call_1"))
        agent.send(message("usage_update", used=20000, size=1000000))
        agent.send(text_chunk("agent_message_chunk", "ANSWER"))
        await settle(c, t)
        assert t.turn_open and ("thinking", "THINKING") in t.turn.timeline
        _reply(agent, 0, "prompt_reply_end_turn")
        await settle(c, t)
        assert not t.turn_open
        summary = _find(c, "🔧 1")
        answer = _find(c, "ANSWER")
        assert answer > summary
        assert "ctx 2% (20.0k)" in dict(screen(c))[summary]
        assert "🧠 THINKING" in c.bot.rich.get(summary, dict(screen(c))[summary])
        assert agent.returncode == -9  # idle, no job: the process ends
        await t.on_close()

    asyncio.run(scenario())


def test_an_earlier_prompts_early_reply_folds_the_next_prompt(tmp_path):
    """A message sent mid-turn: agy replies to the earlier prompt at once (the fold
    signal) — the ack goes, the card moves below the message, and the turn ends on the
    newer prompt's reply, its output the bottom-most."""

    async def scenario():
        c = client(tmp_path)
        t = window(c, tmp_path)
        await t.submit("first", reply_to=_user(c, "first"))
        agent = t.agents[0]
        agent.send(text_chunk("agent_thought_chunk", "working"))
        await settle(c, t)
        card = t.turn.card_id
        second = _user(c, "also say banana")
        await t.submit("also say banana", reply_to=second)
        await settle(c, t)
        ack = t.pending_writes[-1]["bubble_id"]
        assert ack is not None and ack not in c.bot.deleted
        _reply(agent, 0, "prompt_reply_folded")  # the early reply
        await settle(c, t)
        assert ack in c.bot.deleted
        assert card in c.bot.deleted and t.turn.card_id > second
        assert t.turn_open and t.pending_writes == []
        agent.send(text_chunk("agent_message_chunk", "BANANA"))
        _reply(agent, 1, "prompt_reply_end_turn")
        await settle(c, t)
        assert not t.turn_open
        assert _find(c, "BANANA") > second
        assert last_message(c).startswith("BANANA")
        await t.on_close()

    asyncio.run(scenario())


def test_cancel_sends_session_cancel_and_the_cancelled_reply_ends_the_turn(tmp_path):
    async def scenario():
        c = client(tmp_path)
        t = window(c, tmp_path)
        await t.submit("sleep long", reply_to=_user(c, "sleep long"))
        agent = t.agents[0]
        agent.send(text_chunk("agent_thought_chunk", "waiting"))
        await settle(c, t)
        await t.cancel(None)
        await settle(c, t)
        assert agent.requests("session/cancel") == [
            {"jsonrpc": "2.0", "method": "session/cancel", "params": {"sessionId": SESSION}}
        ]
        assert t.cancel_requested
        _reply(agent, 0, "prompt_reply_cancelled")
        await settle(c, t)
        assert not t.turn_open
        assert "(cancelled)" in texts(c)
        assert "✖️ cancelled" in texts(c)
        await t.on_close()

    asyncio.run(scenario())


async def _turn_with_job(c, t, prompt="start it"):
    """A prompted turn whose `run_command` is still running at its reply: a job."""
    await t.submit(prompt, reply_to=_user(c, prompt))
    agent = t.agents[-1]
    agent.send(message("job_tool_call"))
    agent.send(text_chunk("agent_message_chunk", "started"))
    _reply(agent, len(agent.prompt_ids()) - 1, "prompt_reply_end_turn")
    await settle(c, t)
    return agent


def test_a_job_finishing_opens_a_wake_turn_that_ends_on_the_transcript_marker(
    tmp_path, monkeypatch
):
    """The job's `completed` update ends it (✓) and opens a wake turn; ACP sends no end
    for it: the transcript's next `PLANNER_RESPONSE` without tool calls ends it, its
    content the answer."""
    fast_transcript(monkeypatch)

    async def scenario():
        c = client(tmp_path)
        t = window(c, tmp_path)
        agent = await _turn_with_job(c, t)
        job_id = ACP["job_tool_call"]["params"]["update"]["toolCallId"]
        assert t.background_tasks[job_id]["done"] is None
        assert agent.returncode is None  # kept for the running job
        started = _find(c, "started")
        agent.send(message("job_tool_call_completed"))
        await settle(c, t)
        assert t.background_tasks[job_id]["done"] == "✓"
        assert t.turn_open and t.turn.prompt_id is None and t.turn.card_id > started
        write_steps(t, step("system_task_finished"))
        agent.send(text_chunk("agent_message_chunk", "WAKE ANSWER"))
        await settle(c, t)
        assert t.turn_open  # no marker yet
        write_steps(t, step("planner_final", content="WAKE ANSWER"))
        await settle(c, t, rounds=8)
        assert not t.turn_open
        assert last_message(c).startswith("WAKE ANSWER")
        await t.on_close()

    asyncio.run(scenario())


def test_a_wake_turn_ending_in_an_error_step_ends(tmp_path, monkeypatch):
    """A failing wake turn: a trailing error step ends it."""
    fast_transcript(monkeypatch)

    async def scenario():
        c = client(tmp_path)
        t = window(c, tmp_path)
        agent = await _turn_with_job(c, t)
        agent.send(message("job_tool_call_completed"))
        await settle(c, t)
        assert t.turn_open
        write_steps(t, step("system_task_finished"), step("generic_error_denied"))
        await settle(c, t, rounds=8)
        assert not t.turn_open
        assert any("agy ended with an error (error)" in text for text in texts(c))
        await t.on_close()

    asyncio.run(scenario())


def test_a_job_finishing_during_a_prompted_turn_opens_no_wake_turn(tmp_path, monkeypatch):
    """The job ends while a prompted turn runs: the panel marks it, no second turn
    opens, the transcript marker is ignored while the prompt is outstanding, and the
    prompt's reply ends the one turn."""
    fast_transcript(monkeypatch)

    async def scenario():
        c = client(tmp_path)
        t = window(c, tmp_path)
        agent = await _turn_with_job(c, t)
        await t.submit("next", reply_to=_user(c, "next"))
        card = t.turn.card_id
        await settle(c, t)
        card = t.turn.card_id
        agent.send(message("job_tool_call_completed"))
        write_steps(t, step("system_task_finished"), step("planner_final", content="x"))
        await settle(c, t, rounds=8)
        assert t.turn_open and t.turn.card_id == card  # the same turn, still open
        agent.send(text_chunk("agent_message_chunk", "NEXT DONE"))
        _reply(agent, 1, "prompt_reply_end_turn")
        await settle(c, t)
        assert not t.turn_open
        assert last_message(c).startswith("NEXT DONE")
        assert sum("NEXT DONE" in text for text in texts(c)) == 1
        await t.on_close()

    asyncio.run(scenario())


def test_two_jobs_finishing_together_run_one_wake_turn(tmp_path, monkeypatch):
    """Both jobs end; the first completion opens the wake turn, the second joins it;
    one marker ends it."""
    fast_transcript(monkeypatch)

    async def scenario():
        c = client(tmp_path)
        t = window(c, tmp_path)
        await t.submit("start both", reply_to=_user(c, "start both"))
        agent = t.agents[0]
        agent.send(message("job_tool_call", toolCallId="call_a"))
        agent.send(message("job_tool_call", toolCallId="call_b"))
        _reply(agent, 0, "prompt_reply_end_turn")
        await settle(c, t)
        assert len(t.background_tasks) == 2
        agent.send(message("job_tool_call_completed", toolCallId="call_a"))
        agent.send(message("job_tool_call_completed", toolCallId="call_b"))
        await settle(c, t)
        assert [j["done"] for j in t.background_tasks.values()] == ["✓", "✓"]
        cards = [mid for mid, text in screen(c) if text.startswith(("✶", "✸", "✹", "✺"))]
        assert len(cards) == 1
        write_steps(t, step("system_task_finished"), step("planner_final", content="BOTH"))
        await settle(c, t, rounds=8)
        assert not t.turn_open
        assert last_message(c).startswith("BOTH")
        await t.on_close()

    asyncio.run(scenario())


def test_a_permission_ask_is_denied_and_the_denied_tool_shows(tmp_path):
    """`default` mode: the ask is answered with the deny option at once; the tool's
    failed update marks it denied in the timeline, and the turn ends with no reply."""

    async def scenario():
        c = client(tmp_path, permission_mode="default")
        t = window(c, tmp_path)
        await t.submit("touch it", reply_to=_user(c, "touch it"))
        agent = t.agents[0]
        ask = message("request_permission_default")
        tool = ask["params"]["toolCall"]
        agent.send(
            message(
                "tool_call_in_progress",
                **{k: tool[k] for k in ("toolCallId", "rawInput", "args", "title", "name")},
            )
        )
        agent.send(ask)
        await settle(c, t)
        answer = [m for m in agent.received if m.get("id") == ask["id"] and "result" in m]
        assert answer == [
            {
                "jsonrpc": "2.0",
                "id": ask["id"],
                "result": {"outcome": {"outcome": "selected", "optionId": "deny"}},
            }
        ]
        agent.send(message("tool_call_failed_denied"))
        _reply(agent, 0, "prompt_reply_end_turn")
        await settle(c, t)
        timeline = " ".join(c.bot.rich.values()) + " ".join(texts(c))
        assert (
            "touch c7-file &amp;&amp; ls -l c7-file" in timeline
            or "touch c7-file && ls -l c7-file" in timeline
        )
        assert DENIED in timeline
        assert "(no reply)" in texts(c)
        await t.on_close()

    asyncio.run(scenario())


def test_a_permission_ask_in_auto_mode_is_denied_too(tmp_path):
    async def scenario():
        c = client(tmp_path)  # auto
        t = window(c, tmp_path)
        await t.submit("go", reply_to=_user(c, "go"))
        agent = t.agents[0]
        ask = message("request_permission")
        agent.send(ask)
        await settle(c, t)
        answer = [m for m in agent.received if m.get("id") == ask["id"] and "result" in m]
        assert answer[0]["result"]["outcome"]["optionId"] == "deny"
        await t.on_close()

    asyncio.run(scenario())


def test_revive_loads_the_session_and_drops_its_history_replay(tmp_path):
    """After a restart the saved session is loaded, not created: the history agy
    replays before the load reply opens no turn and posts nothing; the note is not
    resent while it is unchanged."""

    async def scenario():
        session_db(tmp_path)
        c = client(tmp_path)
        replay = [
            message("user_message_chunk"),
            text_chunk("agent_thought_chunk", "OLD THINKING"),
            text_chunk("agent_message_chunk", "OLD ANSWER"),
            message("usage_update"),
        ]
        first = window(c, tmp_path)
        await first.submit("hello", reply_to=_user(c, "hello"))
        note_hash = first.saved["note_hash"]
        await first.on_close()
        t = window(c, tmp_path, thread="556", replay=replay)
        t.saved["session"] = {"session_id": SESSION, "workspace": str(tmp_path)}
        t.saved["note_hash"] = note_hash
        await t.on_revive()
        await t.submit("again", reply_to=None)
        agent = t.agents[0]
        await settle(c, t)
        load = agent.requests("session/load")[0]["params"]
        assert load == {"sessionId": SESSION, "cwd": str(tmp_path), "mcpServers": []}
        assert agent.requests("session/new") == []
        shown = texts(c, "556")
        assert not any("OLD" in text for text in shown)
        assert "OLD" not in "".join(t.turn.thinking_parts + t.turn.preview_parts)
        assert len(shown) == 1  # the new turn's card only
        await t.on_close()

    asyncio.run(scenario())


def test_a_changed_note_is_resent_on_load(tmp_path):
    async def scenario():
        session_db(tmp_path)
        c = client(tmp_path)
        t = window(c, tmp_path)
        t.session_id = SESSION
        t.saved["note_hash"] = "an older note"
        await t.submit("again")
        load = t.agents[0].requests("session/load")[0]["params"]
        assert "systemPrompt" in load["_meta"]["antigravity"]
        await t.on_close()

    asyncio.run(scenario())


def test_a_process_exit_flags_each_queued_message_not_delivered(tmp_path):
    async def scenario():
        c = client(tmp_path)
        t = window(c, tmp_path)
        await t.submit("first", reply_to=_user(c, "first"))
        second = _user(c, "second")
        await t.submit("second", reply_to=second)
        await settle(c, t)
        ack = t.pending_writes[-1]["bubble_id"]
        t.agents[0].exit()
        await settle(c, t)
        assert ack in c.bot.deleted
        assert UNDELIVERED_NOTE in texts(c)
        assert not t.busy and t.proc is None
        await t.on_close()

    asyncio.run(scenario())


def test_a_usage_update_between_turns_opens_no_turn(tmp_path):
    async def scenario():
        c = client(tmp_path)
        t = window(c, tmp_path)
        agent = await _turn_with_job(c, t)
        before = len(screen(c))
        agent.send(message("usage_update", used=1, size=2000000))
        agent.send(message("session_info_update", title="Named by agy"))
        await settle(c, t)
        assert not t.turn_open and len(screen(c)) == before
        assert t.context_window == 2000000
        assert t.title == "Named by agy" and t.saved["base_name"] == "Named by agy"
        await t.on_close()

    asyncio.run(scenario())


def test_a_prompt_sent_during_a_wake_turn_folds_into_it(tmp_path, monkeypatch):
    """A message sent while a wake turn runs is acked, then folded (agy always folds):
    its reply ends the turn, the ack gone and the output below the message; the
    transcript marker does not end the turn while the prompt is outstanding."""
    fast_transcript(monkeypatch)

    async def scenario():
        c = client(tmp_path)
        t = window(c, tmp_path)
        agent = await _turn_with_job(c, t)
        agent.send(message("job_tool_call_completed"))
        await settle(c, t)
        assert t.turn_open and t.turn.prompt_id is None
        later = _user(c, "and then?")
        await t.submit("and then?", reply_to=later)
        await settle(c, t)
        ack = t.pending_writes[-1]["bubble_id"]
        write_steps(t, step("system_task_finished"), step("planner_final", content="x"))
        await settle(c, t, rounds=8)
        assert t.turn_open  # the prompt is outstanding
        agent.send(text_chunk("agent_message_chunk", "THEN"))
        _reply(agent, 1, "prompt_reply_end_turn")
        await settle(c, t)
        assert not t.turn_open and not t.pending_writes
        assert ack in c.bot.deleted
        assert _find(c, "THEN") > later and last_message(c).startswith("THEN")
        assert UNDELIVERED_NOTE not in texts(c)
        await t.on_close()

    asyncio.run(scenario())


def test_a_wake_turn_a_prompt_folds_into_counts_the_wake_steps_tokens_too(tmp_path, monkeypatch):
    """The captured order (wake_fold_cancel round A): the wake answer's step (15 output
    tokens), then the folded prompt's `USER_INPUT` step, then the prompt's reply (2,
    only its own step's), and its own step written after the reply. The summary counts
    the wake's 15 plus the reply's 2."""
    fast_transcript(monkeypatch)

    async def scenario():
        c = client(tmp_path)
        t = window(c, tmp_path)
        agent = await _turn_with_job(c, t)
        before = len(texts(c))
        agent.send(message("job_tool_call_completed"))
        await settle(c, t)
        agent.send(text_chunk("agent_message_chunk", "The background task finished"))
        later = _user(c, "Also say kiwi.")
        await t.submit("Also say kiwi.", reply_to=later)
        await settle(c, t)
        write_steps(
            t,
            step("system_task_finished"),
            step("planner_final", content="wake", output_tokens=15),
            step("user_input", content="<USER_REQUEST>\nAlso say kiwi.\n</USER_REQUEST>"),
        )
        await settle(c, t, rounds=8)
        agent.send(text_chunk("agent_message_chunk", "kiwi"))
        _reply(agent, 1, "prompt_reply_end_turn", usage={"outputTokens": 2})
        await settle(c, t)
        write_steps(t, step("planner_final", content="kiwi", output_tokens=2))
        await settle(c, t, rounds=8)
        summary = [x for x in texts(c)[before:] if "↓" in x]
        assert summary and "↓ 17" in summary[-1].splitlines()[0]
        await t.on_close()

    asyncio.run(scenario())


def test_a_folded_prompts_own_steps_before_its_reply_count_once(tmp_path, monkeypatch):
    """The folded prompt runs a tool: its `PLANNER_RESPONSE` with tool calls (5 output
    tokens) lands after its `USER_INPUT` step and before its reply, which counts it
    (reply 7). The wake's 15 count from the transcript, the prompt's only through the
    reply: 15 + 7, never 15 + 5 + 7."""
    fast_transcript(monkeypatch)

    async def scenario():
        c = client(tmp_path)
        t = window(c, tmp_path)
        agent = await _turn_with_job(c, t)
        before = len(texts(c))
        agent.send(message("job_tool_call_completed"))
        await settle(c, t)
        agent.send(text_chunk("agent_message_chunk", "The background task finished"))
        later = _user(c, "Now list the files.")
        await t.submit("Now list the files.", reply_to=later)
        await settle(c, t)
        write_steps(
            t,
            step("system_task_finished"),
            step("planner_final", content="wake", output_tokens=15),
            step("user_input", content="<USER_REQUEST>\nNow list the files.\n</USER_REQUEST>"),
            step("planner_tool_calls", output_tokens=5),
        )
        await settle(c, t, rounds=8)
        agent.send(text_chunk("agent_message_chunk", "two files"))
        _reply(agent, 1, "prompt_reply_end_turn", usage={"outputTokens": 7})
        await settle(c, t)
        summary = [x for x in texts(c)[before:] if "↓" in x]
        assert summary and "↓ 22" in summary[-1].splitlines()[0]
        await t.on_close()

    asyncio.run(scenario())


def test_an_attached_image_also_goes_as_an_image_block(tmp_path):
    async def scenario():
        c = client(tmp_path)
        t = window(c, tmp_path)
        png = tmp_path / "shot.png"
        png.write_bytes(b"\x89PNG")
        doc = tmp_path / "notes.txt"
        doc.write_text("x")
        text = f"look\n\n{ATTACHED_HEAD}\n{png}\n{doc}\n]"
        await t.submit(text)
        blocks = t.agents[0].requests("session/prompt")[0]["params"]["prompt"]
        assert blocks == [
            {"type": "text", "text": text},
            {
                "type": "image",
                "mimeType": "image/png",
                "data": base64.b64encode(b"\x89PNG").decode(),
            },
        ]
        await t.on_close()

    asyncio.run(scenario())


def test_cancel_during_a_wake_turn_ends_it_as_cancelled(tmp_path):
    """Round B of the capture: a job, the prompt's reply, the job's end (a wake turn);
    `session/cancel` then gets nothing back — the window ends the turn itself."""

    async def scenario():
        c = client(tmp_path)
        t = window(c, tmp_path)
        await t.submit("start b", reply_to=_user(c, "start b"))
        agent = t.agents[0]
        agent.send(message("wake_cancel_job_tool_call"))
        agent.send({**ACP["wake_cancel_prompt_reply"], "id": agent.prompt_ids()[0]})
        await settle(c, t)
        agent.send(message("wake_cancel_job_completed"))
        await settle(c, t)
        assert t.turn_open and t.turn.prompt_id is None and not t.prompts
        await t.cancel(None)
        await settle(c, t)
        assert agent.requests("session/cancel")
        assert not t.turn_open and not t.busy
        assert "(cancelled)" in texts(c)
        assert "✖️ cancelled" in texts(c)
        assert texts(c).index("(cancelled)") > texts(c).index("✖️ cancelled")  # below the note
        await t.on_close()

    asyncio.run(scenario())


def test_cancel_during_a_prompted_turn_waits_for_its_reply(tmp_path):
    async def scenario():
        c = client(tmp_path)
        t = window(c, tmp_path)
        await t.submit("long", reply_to=_user(c, "long"))
        await settle(c, t)
        await t.cancel(None)
        await settle(c, t)
        assert t.turn_open  # the cancelled reply ends it
        agent = t.agents[0]
        agent.send({**ACP["prompt_reply_cancelled"], "id": agent.prompt_ids()[0]})
        await settle(c, t)
        assert not t.turn_open and "(cancelled)" in texts(c)
        await t.on_close()

    asyncio.run(scenario())


def test_a_message_right_after_an_idle_end_runs_in_a_fresh_process(tmp_path):
    """A real process is reaped a moment after its kill, and its reader tears down only
    then: a message in that gap must wait for the old teardown and run in a new process,
    never write to the dying one or have its turn wiped by that teardown."""

    async def scenario():
        c = client(tmp_path)
        t = window(c, tmp_path)
        await t.submit("hi", reply_to=_user(c, "hi"))
        await settle(c, t)
        old = t.agents[-1]

        killed = asyncio.Event()

        def slow_kill():  # the exit is noticed a few ms after the kill
            if old.returncode is None and not killed.is_set():
                killed.set()
                asyncio.get_running_loop().call_later(0.02, FakeAgent.kill.__get__(old))

        old.kill = slow_kill
        _reply(old, 0, "prompt_reply_end_turn")
        await asyncio.wait_for(killed.wait(), 5)  # the idle turn end killed the process
        assert old.returncode is None  # its exit not noticed yet
        await t.submit("again", reply_to=_user(c, "again"))
        await settle(c, t)
        new = t.agents[-1]
        assert new is not old
        assert any("again" in json.dumps(m) for m in new.requests("session/prompt"))
        assert not any("again" in json.dumps(m) for m in old.requests("session/prompt"))
        assert t.turn_open and t.pending_writes  # the old teardown left the new turn alone
        assert not any("exited" in x for x in texts(c))
        _reply(new, 0, "prompt_reply_end_turn")
        await settle(c, t)
        await t.on_close()

    asyncio.run(scenario())
