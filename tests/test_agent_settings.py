"""App-wide and per-window agent settings read from the store at each use, so a write
made outside the running bot (`tgforge model|mode|window|workspaces`) is seen without a
restart and never undone by the bot's own saves; and the CLI commands that write them."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from agy_fakes import ACP, message
from agy_fakes import client as agy_client
from agy_fakes import settle as agy_settle
from agy_fakes import window as agy_window
from claude_fakes import THREAD, FakeProc, assistant_text, drain, result, texts, window

from tgforge import cli
from tgforge.base.kernel import AppDB, window_ns
from tgforge.plugins.agent.window import write_roots
from tgforge.plugins.agy import Agy
from tgforge.plugins.claude import Claude
from tgforge.testing import TestClient

MODELS = [["Opus", "claude-opus"], ["Sonnet", "claude-sonnet"]]


def _claude(tmp_path, **plugin_args):
    c = TestClient(Claude(**plugin_args), home=str(tmp_path))
    plugin = c.core.plugin_by_id["claude"]
    plugin._load()  # what startup does
    return c, plugin


def _claude_spawns(t, *scripts):
    """Each Claude spawn starts the next scripted process; returns [(cmd, env)]."""
    queue, spawned = list(scripts), []

    async def spawn(*cmd, **kwargs):
        spawned.append((list(cmd), kwargs.get("env") or {}))
        return FakeProc(queue.pop(0))

    t.spawn = spawn
    return spawned


def _flag(cmd, name):
    return cmd[cmd.index(name) + 1] if name in cmd else None


def _run_cli(capsys, *argv) -> str:
    cli.main(list(argv))
    return capsys.readouterr().out


# ── change 4: settings read at each use ────────────────────────────


def test_an_outside_write_of_each_plugin_setting_is_seen_without_a_restart(tmp_path):
    async def scenario():
        c, plugin = _claude(tmp_path)
        root = tmp_path / "ws"
        root.mkdir()
        # the running bot has read each setting before the outside write
        assert (plugin.models, plugin.default_model, plugin.permission_mode) == ([], None, "auto")
        assert (plugin.accounts, plugin.roots) == ({}, [])
        outside = cli._agent_plugin("claude", tmp_path)  # another connection to state.db
        outside.add_model("Opus", "claude-opus")
        outside.set_default_model("claude-opus")
        outside.set_permission_mode("plan")
        outside.set_account("work", str(tmp_path / ".claude-work"))
        write_roots(tmp_path, [str(root)])
        assert plugin.models == [["Opus", "claude-opus"]]
        assert plugin.default_model == "claude-opus"
        assert plugin.permission_mode == "plan"
        assert plugin.accounts == {"work": str(tmp_path / ".claude-work")}
        assert plugin.workspaces == [root.resolve()]
        t = window(c, tmp_path)
        shown = []

        async def menu(title, options):
            shown.append(options)

        t.menu = menu
        await t._switch_model()  # the picker
        assert ("Opus ✓", "claude-opus") in shown[0]
        status = await t.status(None)
        assert "model claude-opus" in status and "plan" in status
        spawned = _claude_spawns(t, [assistant_text("ok"), result("ok")])
        await t.submit("hi")
        await drain(c, t)
        cmd = spawned[0][0]
        assert _flag(cmd, "--model") == "claude-opus" and _flag(cmd, "--permission-mode") == "plan"

    asyncio.run(scenario())


def test_a_bot_save_of_one_setting_keeps_an_outside_write_of_another(tmp_path):
    c, plugin = _claude(tmp_path, models=MODELS)
    outside = cli._agent_plugin("claude", tmp_path)
    outside.set_permission_mode("plan")
    plugin.add_model("Haiku", "claude-haiku")  # the bot's own edit of the model list
    assert plugin.permission_mode == "plan" and outside.permission_mode == "plan"
    outside.set_default_model("claude-sonnet")
    plugin.set_permission_mode("auto")
    plugin.set_account("work", "/x")
    assert outside.default_model == "claude-sonnet"
    assert outside.models == [*MODELS, ["Haiku", "claude-haiku"]]
    outside.add_model("Extra", "x")
    plugin.set_default_model("claude-opus")
    assert ["Extra", "x"] in plugin.models
    # the window: its own record saves never touch the model / effort keys
    t = window(c, tmp_path)
    t._save()
    store = AppDB(tmp_path / "state.db").saved(window_ns("claude", THREAD))
    store["model"] = "claude-opus"
    t.context_window = 1  # any change the window saves
    t._save()
    assert store["model"] == "claude-opus" and t.model == "claude-opus"


def test_the_app_model_list_is_recorded_at_startup_for_the_cli(tmp_path):
    _claude(tmp_path, models=MODELS)
    assert cli._agent_plugin("claude", tmp_path).models == MODELS


def test_a_model_and_effort_saved_inside_the_session_record_move_to_their_keys(tmp_path):
    async def scenario():
        c, _ = _claude(tmp_path)
        t = window(c, tmp_path)
        t.saved["session"] = {"workspace": str(tmp_path), "model": "m", "effort": "high"}
        await t.on_revive()
        await t.on_close()
        assert (t.saved["model"], t.saved["effort"]) == ("m", "high")
        t._save()
        assert "model" not in t.saved["session"] and t.model == "m"

    asyncio.run(scenario())


# ── change 5: tgforge model ────────────────────────────────────────


def test_model_commands_print_the_list_and_store_the_result(tmp_path, capsys):
    _claude(tmp_path, models=MODELS)
    home = ("--home", str(tmp_path))
    out = _run_cli(capsys, "model", *home)
    assert (
        out == "models offered at /claude:\n  1. Opus — claude-opus\n  2. Sonnet — claude-sonnet\n"
    )
    out = _run_cli(capsys, "model", "add", "Haiku|claude-haiku", *home)
    assert out.startswith("added 'Haiku' → claude-haiku\n") and "  3. Haiku — claude-haiku" in out
    out = _run_cli(capsys, "model", "default", "claude-sonnet", *home)
    assert "default model → claude-sonnet" in out
    assert "  2. Sonnet — claude-sonnet (default)" in out
    out = _run_cli(capsys, "model", "rm", "1", *home)
    assert out.startswith("removed 'Opus'\n")
    out = _run_cli(capsys, "model", "rm", "nope", *home)
    assert out.startswith("no model matching 'nope'\n")
    stored = cli._agent_plugin("claude", tmp_path)
    assert stored.models == [["Sonnet", "claude-sonnet"], ["Haiku", "claude-haiku"]]
    assert stored.default_model == "claude-sonnet"
    out = _run_cli(capsys, "model", "reset", *home)
    assert out.startswith("model list reset to defaults\n")
    assert cli._agent_plugin("claude", tmp_path).models == MODELS  # the app's list


def test_model_add_with_no_value_is_refused(tmp_path, capsys):
    _claude(tmp_path, models=MODELS)
    with pytest.raises(SystemExit, match="needs a value"):
        cli.main(["model", "add", "--home", str(tmp_path)])
    assert cli._agent_plugin("claude", tmp_path).models == MODELS


def test_model_takes_the_agy_plugin(tmp_path, capsys):
    TestClient(Agy(), home=str(tmp_path))
    out = _run_cli(
        capsys, "model", "add", "Flash|gemini-3.8-flash", "--plugin", "agy", "--home", str(tmp_path)
    )
    assert "models offered at /agy:" in out
    assert cli._agent_plugin("agy", tmp_path).models == [["Flash", "gemini-3.8-flash"]]
    assert cli._agent_plugin("claude", tmp_path).models == []


# ── change 5: tgforge mode ─────────────────────────────────────────


def test_mode_prints_and_sets_the_plugins_mode(tmp_path, capsys):
    _claude(tmp_path)
    assert _run_cli(capsys, "mode", "--home", str(tmp_path)) == "claude mode: auto\n"
    out = _run_cli(capsys, "mode", "plan", "--home", str(tmp_path))
    assert out == "claude mode → plan (each window's next process runs it)\n"
    assert cli._agent_plugin("claude", tmp_path).permission_mode == "plan"
    out = _run_cli(capsys, "mode", "skip", "--plugin", "agy", "--home", str(tmp_path))
    assert out.startswith("agy mode → skip")
    assert cli._agent_plugin("claude", tmp_path).permission_mode == "plan"


def test_an_unknown_mode_is_refused_and_nothing_written(tmp_path):
    _claude(tmp_path)
    with pytest.raises(SystemExit, match="unknown mode 'skip'"):
        cli.main(["mode", "skip", "--home", str(tmp_path)])  # an agy mode, not Claude's
    assert "permission_mode" not in cli._agent_plugin("claude", tmp_path).saved


# ── change 5: tgforge window ───────────────────────────────────────


def _window_store(tmp_path, plugin_id, key=THREAD):
    store = AppDB(tmp_path / "state.db").saved(window_ns(plugin_id, key))
    store.setdefault("session", {"workspace": str(tmp_path)})
    return store


def test_window_prints_and_sets_its_model_and_effort(tmp_path, capsys, monkeypatch):
    _claude(tmp_path, models=MODELS)
    store = _window_store(tmp_path, "claude")
    monkeypatch.setenv("TGFORGE_WINDOW", THREAD)
    monkeypatch.setenv("TGFORGE_PLUGIN", "claude")
    home = ("--home", str(tmp_path))
    assert _run_cli(capsys, "window", *home) == "model default · effort default\n"
    out = _run_cli(capsys, "window", "--model", "Sonnet", "--effort", "high", *home)
    assert out == "model claude-sonnet · effort high\n"  # a label stores its value
    assert (store["model"], store["effort"]) == ("claude-sonnet", "high")
    out = _run_cli(capsys, "window", "--model", "default", *home)
    assert out == "model default · effort high\n" and "model" not in store


def test_a_refused_window_value_writes_nothing(tmp_path, monkeypatch):
    _claude(tmp_path, models=MODELS)
    store = _window_store(tmp_path, "claude")
    monkeypatch.setenv("TGFORGE_WINDOW", THREAD)
    home = ["--home", str(tmp_path)]
    with pytest.raises(SystemExit, match="not in the claude model list"):
        cli.main(["window", "--model", "gpt-x", "--effort", "high", *home])
    with pytest.raises(SystemExit, match="effort 'xhigh' is not one of: low, medium, high"):
        cli.main(["window", "--model", "claude-opus", "--effort", "xhigh", *home])
    assert "model" not in store and "effort" not in store
    with pytest.raises(SystemExit, match="no claude window '999'"):
        cli.main(["window", "--window", "999", "--model", "claude-opus", *home])
    monkeypatch.delenv("TGFORGE_WINDOW")
    with pytest.raises(SystemExit, match="no window"):
        cli.main(["window", *home])


def test_the_window_key_resolves_to_the_plugin_tgforge_plugin_names(tmp_path, capsys, monkeypatch):
    TestClient(Claude(models=MODELS), Agy(), home=str(tmp_path))
    claude_store = _window_store(tmp_path, "claude")
    agy_store = _window_store(tmp_path, "agy")
    claude_store["model"] = "claude-opus"
    agy_store["model"] = "gemini-3.8-flash"
    monkeypatch.setenv("TGFORGE_WINDOW", THREAD)
    monkeypatch.setenv("TGFORGE_PLUGIN", "agy")
    home = ("--home", str(tmp_path))
    assert _run_cli(capsys, "window", *home) == "model gemini-3.8-flash · effort default\n"
    assert _run_cli(capsys, "window", "--effort", "xhigh", *home).endswith("effort xhigh\n")
    assert agy_store["effort"] == "xhigh" and "effort" not in claude_store
    out = _run_cli(capsys, "window", "--plugin", "claude", *home)
    assert out == "model claude-opus · effort default\n"


def test_an_agy_window_model_is_checked_against_the_models_agy_lists(tmp_path, monkeypatch):
    async def scenario():
        c = agy_client(tmp_path)
        t = agy_window(c, tmp_path)
        await t.submit("hi", reply_to=None)  # session/new lists the models
        await agy_settle(c, t)
        await t.on_close()

    asyncio.run(scenario())
    _window_store(tmp_path, "agy")
    monkeypatch.setenv("TGFORGE_WINDOW", THREAD)
    monkeypatch.setenv("TGFORGE_PLUGIN", "agy")
    cli.main(["window", "--model", "gemini-3.8-flash", "--home", str(tmp_path)])
    with pytest.raises(SystemExit, match="not in the agy model list: gemini-4-neon"):
        cli.main(["window", "--model", "claude-opus", "--home", str(tmp_path)])


def test_both_backends_name_their_plugin_next_to_the_window(tmp_path):
    async def scenario():
        c, _ = _claude(tmp_path)
        t = window(c, tmp_path)
        spawned = _claude_spawns(t, [assistant_text("ok"), result("ok")])
        await t.submit("hi")
        await drain(c, t)
        env = spawned[0][1]
        assert (env["TGFORGE_WINDOW"], env["TGFORGE_PLUGIN"]) == (THREAD, "claude")
        ac = agy_client(tmp_path / "agy")
        (tmp_path / "agy").mkdir(exist_ok=True)
        at = agy_window(ac, tmp_path)
        captured = {}
        spawn = at.spawn

        async def spy(*args, **kwargs):
            if "acp" in args:
                captured.update(kwargs["env"])
            return await spawn(*args, **kwargs)

        at.spawn = spy
        await at.submit("hi", reply_to=None)
        await at.on_close()
        assert (captured["TGFORGE_WINDOW"], captured["TGFORGE_PLUGIN"]) == (THREAD, "agy")

    asyncio.run(scenario())


def test_a_claude_window_write_during_a_turn_applies_at_the_next_turn(
    tmp_path, monkeypatch, capsys
):
    """The session runs `tgforge window` during its own turn: that turn keeps its
    settings; the next one spawns with the new model and effort, after the notes."""
    monkeypatch.setenv("TGFORGE_WINDOW", THREAD)
    monkeypatch.setenv("TGFORGE_PLUGIN", "claude")

    async def scenario():
        c, _ = _claude(tmp_path, models=MODELS)
        t = window(c, tmp_path)
        t._save()
        hold = asyncio.Event()
        spawned = _claude_spawns(
            t,
            [hold, assistant_text("one"), result("one")],
            [assistant_text("two"), result("two")],
        )
        await t.submit("first")
        await drain(c, t)
        cli.main(["window", "--model", "claude-opus", "--effort", "high", "--home", str(tmp_path)])
        t._save()  # the window's own saves during the turn
        hold.set()
        await drain(c, t)
        await asyncio.sleep(0)
        assert t.saved["model"] == "claude-opus" and t.saved["effort"] == "high"
        first = spawned[0][0]
        assert _flag(first, "--model") is None and _flag(first, "--effort") is None
        assert not any("model →" in x for x in texts(c))  # nothing applied mid-turn
        await t.submit("second")
        await drain(c, t)
        second = spawned[1][0]
        assert _flag(second, "--model") == "claude-opus" and _flag(second, "--effort") == "high"
        shown = texts(c)
        notes = [shown.index("🧠 model → claude-opus"), shown.index("⚡ effort → high")]
        assert shown.index("one") < min(notes) and max(notes) < shown.index("two")
        _claude_spawns(t, [assistant_text("three"), result("three")])
        await t.submit("third")  # applied once: no second note
        await drain(c, t)
        assert sum("model →" in x for x in texts(c)) == 1

    asyncio.run(scenario())


class _ReapedProc(FakeProc):
    """An asyncio process: a kill shows in `returncode` only once `wait` reaps it."""

    def kill(self):
        self.killed = True
        self.stdout.close()

    async def wait(self):
        self.returncode = -9
        return self.returncode


def test_an_idle_claude_process_is_dropped_for_an_outside_model_write(tmp_path):
    """A process kept between turns (a job ran) runs the old model: the next turn ends
    it and spawns a fresh one after its teardown."""

    async def scenario():
        c, _ = _claude(tmp_path, models=MODELS)
        t = window(c, tmp_path)
        kept = _ReapedProc([asyncio.Event()])  # alive, idle, its reader waiting
        t.proc = kept
        t.reader_task = asyncio.create_task(t._reader())
        await asyncio.sleep(0)
        t.saved["model"] = "claude-opus"  # written from outside
        spawned = _claude_spawns(t, [assistant_text("two"), result("two")])
        await t.submit("next")
        await drain(c, t)
        assert kept.returncode == -9  # the old-model process ended
        assert _flag(spawned[0][0], "--model") == "claude-opus"
        assert "two" in texts(c)

    asyncio.run(scenario())


def test_an_agy_window_write_during_a_turn_applies_at_the_next_turn(tmp_path, monkeypatch):
    monkeypatch.setenv("TGFORGE_WINDOW", THREAD)
    monkeypatch.setenv("TGFORGE_PLUGIN", "agy")

    async def scenario():
        c = agy_client(tmp_path)
        t = agy_window(c, tmp_path)
        t._save()
        await t.submit("first", reply_to=None)
        agent = t.agents[-1]
        await agy_settle(c, t)
        cli.main(
            ["window", "--model", "gemini-3.8-flash", "--effort", "low", "--home", str(tmp_path)]
        )
        t._save()
        await t.submit("more", reply_to=None)  # a prompt sent mid-turn joins this turn
        await agy_settle(c, t)
        for prompt_id in agent.prompt_ids():  # the early reply folds it, the newest ends it
            agent.send({**ACP["prompt_reply_end_turn"], "id": prompt_id})
        await agy_settle(c, t)
        assert not t.turn_open
        mid_turn = [m["params"]["value"] for m in agent.requests("session/set_config_option")]
        assert "gemini-3.8-flash" not in mid_turn and "low" not in mid_turn
        assert not any("model →" in x for x in texts(c))  # nothing applied mid-turn
        await t.submit("second", reply_to=None)
        await agy_settle(c, t)
        agent = t.agents[-1]
        sets = {
            m["params"]["configId"]: m["params"]["value"]
            for m in agent.requests("session/set_config_option")
        }
        assert sets["model"] == "gemini-3.8-flash" and sets["thought_level"] == "low"
        shown = texts(c)
        assert "🧠 model → gemini-3.8-flash" in shown and "⚡ effort → low" in shown
        assert t.saved["model"] == "gemini-3.8-flash"
        await t.on_close()

    asyncio.run(scenario())


def test_the_chat_switches_post_the_same_notes(tmp_path):
    async def scenario():
        c, _ = _claude(tmp_path, models=MODELS)
        t = window(c, tmp_path)
        answers = iter(["claude-opus", "high"])

        async def menu(title, options):
            return next(answers)

        t.menu = menu
        await t._switch_model()
        await t.effort_cmd(SimpleNamespace(args=""))
        await drain(c, t)
        assert texts(c)[-2:] == ["🧠 model → claude-opus", "⚡ effort → high"]
        _claude_spawns(t, [assistant_text("ok"), result("ok")])
        await t.submit("hi")  # the window's own switch is applied: no outside note
        await drain(c, t)
        assert texts(c).count("🧠 model → claude-opus") == 1

    asyncio.run(scenario())


def test_a_live_idle_agy_session_takes_an_outside_write_before_its_next_prompt(tmp_path):
    """A process kept between turns (a job runs) gets the new model through
    `session/set_config_option`, ahead of the next prompt."""

    async def scenario():
        c = agy_client(tmp_path)
        t = agy_window(c, tmp_path)
        await t.submit("start", reply_to=None)
        agent = t.agents[-1]
        agent.send(message("job_tool_call"))
        agent.send({**ACP["prompt_reply_end_turn"], "id": agent.prompt_ids()[-1]})
        await agy_settle(c, t)
        assert agent.returncode is None and not t.busy  # kept for the job
        t.saved["model"] = "gemini-3.8-flash"  # written from outside
        await t.submit("next", reply_to=None)
        await agy_settle(c, t)
        assert t.agents == [agent]  # the same process
        methods = [(m.get("method"), m.get("params", {}).get("value")) for m in agent.received]
        set_at = methods.index(("session/set_config_option", "gemini-3.8-flash"))
        prompts = [i for i, (method, _) in enumerate(methods) if method == "session/prompt"]
        assert prompts[0] < set_at < prompts[1]
        assert "🧠 model → gemini-3.8-flash" in texts(c)
        await t.on_close()

    asyncio.run(scenario())


# ── review 7 ──────────────────────────────────────────────────────


def test_a_reset_after_the_move_stays_reset_across_a_restart(tmp_path):
    """The move takes the values out of the old record, so a later reset to the
    default is not undone by the next revive."""

    async def scenario():
        c, _ = _claude(tmp_path)
        t = window(c, tmp_path)
        t.saved["session"] = {"workspace": str(tmp_path), "model": "m", "effort": "high"}
        await t.on_revive()
        await t.on_close()
        assert "model" not in t.saved["session"] and "effort" not in t.saved["session"]
        t.saved.pop("model")  # `tgforge window --model default --effort default`
        t.saved.pop("effort")
        again = window(c, tmp_path)
        await again.on_revive()
        await again.on_close()
        assert (again.model, again.effort) == (None, None)

    asyncio.run(scenario())


def test_a_key_already_set_wins_over_the_old_record(tmp_path):
    async def scenario():
        c, _ = _claude(tmp_path)
        t = window(c, tmp_path)
        t.saved["session"] = {"workspace": str(tmp_path), "model": "old", "effort": "low"}
        t.saved["model"] = "newer"
        await t.on_revive()
        await t.on_close()
        assert (t.model, t.effort) == ("newer", "low")

    asyncio.run(scenario())


def test_an_outside_write_before_a_restart_still_gets_its_note(tmp_path):
    """`tgforge window --model …`, then a restart before the next turn: the revived
    window still notes the switch and drops the old model's context window."""

    async def scenario():
        c, _ = _claude(tmp_path, models=MODELS)
        t = window(c, tmp_path)
        t.model = "claude-sonnet"
        t.context_window = 1_000_000
        t._save()
        t.saved["model"] = "claude-opus"  # written from outside
        revived = window(c, tmp_path)  # the bot restarted
        await revived.on_revive()
        assert revived.context_window == 1_000_000
        _claude_spawns(revived, [assistant_text("two"), result("two")])
        await revived.submit("next")
        await drain(c, revived)
        shown = texts(c)
        assert shown.index("🧠 model → claude-opus") < shown.index("two")
        assert revived.context_window is None or revived.context_window != 1_000_000

    asyncio.run(scenario())


def test_a_new_default_model_is_noted_in_a_window_on_the_default(tmp_path):
    async def scenario():
        c, plugin = _claude(tmp_path, models=MODELS)
        t = window(c, tmp_path)
        await t.on_revive()  # on the default model
        t.context_window = 200_000
        cli._agent_plugin("claude", tmp_path).set_default_model("claude-opus")  # outside
        spawned = _claude_spawns(t, [assistant_text("two"), result("two")])
        await t.submit("next")
        await drain(c, t)
        assert "🧠 model → default (claude-opus)" in texts(c)
        assert _flag(spawned[0][0], "--model") == "claude-opus"

    asyncio.run(scenario())


def test_a_mid_turn_model_switch_runs_at_the_next_turn_on_a_kept_process(tmp_path):
    """`/model` during a turn that leaves a background job (its process kept): the
    next turn ends the old-model process and spawns the new model, with one note."""

    async def scenario():
        c, _ = _claude(tmp_path, models=MODELS)
        t = window(c, tmp_path)
        await t.on_revive()
        output = tmp_path / "tasks" / "bid9.output"
        output.parent.mkdir()
        output.write_text("")
        launch = {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "tb",
                        "name": "Bash",
                        "input": {"command": "sleep 9", "run_in_background": True},
                    }
                ]
            },
        }
        launched = {
            "type": "user",
            "tool_use_result": {"backgroundTaskId": "bid9"},
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tb",
                        "content": f"Output is being written to: {output}",
                    }
                ]
            },
        }
        from claude_fakes import cancel_task, line

        hold, idle = asyncio.Event(), asyncio.Event()  # `idle`: the process stays up
        spawned = _claude_spawns(
            t,
            [line(launch), line(launched), hold, assistant_text("one"), result("one"), idle],
            [assistant_text("two"), result("two")],
        )
        await t.submit("first")
        await drain(c, t)

        async def menu(title, options):
            return "claude-opus"

        t.menu = menu
        await t._switch_model()  # mid-turn
        hold.set()
        await asyncio.sleep(0.05)
        await drain(c, t)
        assert t.backend_alive() and not t.busy  # kept for its job
        await t.submit("second")
        await drain(c, t)
        assert [_flag(cmd, "--model") for cmd, _ in spawned] == [None, "claude-opus"]
        assert sum("model →" in x for x in texts(c)) == 1
        for task in (t.background_updater_task, t.heartbeat_task, t.mirror_task):
            await cancel_task(task)

    asyncio.run(scenario())


def test_model_and_mode_default_to_the_windows_own_plugin(tmp_path, capsys, monkeypatch):
    """Inside an agy window (`TGFORGE_PLUGIN=agy`) the commands edit agy's settings."""
    _claude(tmp_path)
    monkeypatch.setenv("TGFORGE_PLUGIN", "agy")
    _run_cli(capsys, "model", "add", "Neon|gemini-4-neon", "--home", str(tmp_path))
    assert cli._agent_plugin("agy", tmp_path).models == [["Neon", "gemini-4-neon"]]
    assert cli._agent_plugin("claude", tmp_path).models == []
    assert _run_cli(capsys, "mode", "--home", str(tmp_path)).startswith("agy mode:")


def test_an_unknown_default_model_is_refused(tmp_path):
    _claude(tmp_path, models=MODELS)
    with pytest.raises(SystemExit, match="'claude-typo' is not in the claude model list"):
        cli.main(["model", "default", "claude-typo", "--home", str(tmp_path)])
    assert cli._agent_plugin("claude", tmp_path).default_model is None


def test_an_ended_processs_reader_finishes_its_teardown_before_the_next_spawn(tmp_path):
    async def scenario():
        c, _ = _claude(tmp_path)
        t = window(c, tmp_path)
        finished = []
        real_exited = t._process_exited

        async def slow_exited(*args, **kwargs):
            await asyncio.sleep(0.2)  # e.g. a cancel note's edit still landing
            await real_exited(*args, **kwargs)
            finished.append(True)

        t._process_exited = slow_exited
        kept = _ReapedProc([asyncio.Event()])
        t.proc = kept
        t.reader_task = asyncio.create_task(t._reader())
        await asyncio.sleep(0)
        t.backend_end()
        await asyncio.sleep(0.05)  # the reader is inside its teardown
        _claude_spawns(t, [assistant_text("two"), result("two")])
        await t.submit("next")
        await drain(c, t)
        assert finished == [True]
        assert "two" in texts(c)

    asyncio.run(scenario())
