"""Mid-session config switching: model and effort are per-window and switchable at
runtime (not only at launch), via `/model` and `/effort`; each sets the value and drops
an idle proc so the next message respawns with it. The list editors were renamed to the
singular commands `/model` and `/workspace`; typed `add|rm|reset` still works. See
AGENTS.md → Mid-session config switching."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from tgforge.base.kernel import build_registry
from tgforge.plugins.claude import Claude, ClaudeTopic
from tgforge.testing import TestClient


def _topic(tmp_path, models=None):
    c = TestClient(Claude(models=models), home=str(tmp_path))
    t = c.core._instantiate(ClaudeTopic, 555, "work")
    c.core.owners[555] = "claude"
    return c, t


def test_model_switch_sets_the_model_and_respawns_an_idle_proc(tmp_path):
    async def scenario():
        c, t = _topic(tmp_path, models=[["Sonnet", "claude-sonnet"], ["Opus", "claude-opus"]])
        killed = []
        t.proc = SimpleNamespace(returncode=None, kill=lambda: killed.append(True))
        t.busy = False
        task = asyncio.create_task(t.model_cmd(None))
        await c.pump()
        await c.tap("🔀 Switch model")
        await c.pump()
        await c.tap("Opus")
        await asyncio.wait_for(task, 2)
        assert t.model == "claude-opus"
        assert killed == [True]  # idle proc dropped so the next message respawns

    asyncio.run(scenario())


def test_effort_switch_sets_the_effort_and_respawns_an_idle_proc(tmp_path):
    async def scenario():
        c, t = _topic(tmp_path)
        killed = []
        t.proc = SimpleNamespace(returncode=None, kill=lambda: killed.append(True))
        t.busy = False
        task = asyncio.create_task(t.effort_cmd(None))
        await c.pump()
        await c.tap("high")
        await asyncio.wait_for(task, 2)
        assert t.effort == "high"
        assert killed == [True]

    asyncio.run(scenario())


def test_switch_leaves_a_busy_turn_alone(tmp_path):
    async def scenario():
        c, t = _topic(tmp_path)
        killed = []
        t.proc = SimpleNamespace(returncode=None, kill=lambda: killed.append(True))
        t.busy = True  # a live turn — never kill it
        task = asyncio.create_task(t.effort_cmd(None))
        await c.pump()
        await c.tap("high")
        await asyncio.wait_for(task, 2)
        assert t.effort == "high"
        assert killed == []  # the running turn is untouched

    asyncio.run(scenario())


def test_model_switch_to_default_clears_the_model(tmp_path):
    async def scenario():
        c, t = _topic(tmp_path, models=[["Sonnet", "claude-sonnet"], ["Opus", "claude-opus"]])
        t.model = "claude-opus"  # already on a named model
        killed = []
        t.proc = SimpleNamespace(returncode=None, kill=lambda: killed.append(True))
        t.busy = False
        task = asyncio.create_task(t.model_cmd(None))
        await c.pump()
        await c.tap("🔀 Switch model")
        await c.pump()
        await c.tap("Default")
        await asyncio.wait_for(task, 2)
        assert t.model is None  # back to the CLI default
        assert killed == [True]

    asyncio.run(scenario())


def test_model_typed_add_still_works(tmp_path):
    async def scenario():
        c, t = _topic(tmp_path, models=[["Sonnet", "claude-sonnet"]])

        async def _send(*a, **k):
            return None

        ctx = SimpleNamespace(args="add Opus | claude-opus", send=_send)
        await t.model_cmd(ctx)
        assert ["Opus", "claude-opus"] in t.plugin.models

    asyncio.run(scenario())


def test_commands_renamed_to_singular(tmp_path):
    c = TestClient(Claude(), home=str(tmp_path))
    reg = build_registry(c.core.plugins)
    cmds = reg.classes[ClaudeTopic.id].commands
    assert {"/model", "/effort", "/workspace"} <= set(cmds)
    assert "/models" not in cmds and "/workspaces" not in cmds
