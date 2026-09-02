"""Restart handoff: after a restart the kernel posts "back online" to the recorded
requester thread and calls that window's `on_restarted`. The base hook is a no-op;
the claude window re-enters its session so a paused turn resumes."""

from __future__ import annotations

import asyncio

from tgforge.base.kernel import Topic
from tgforge.plugins.claude import Claude, ClaudeTopic
from tgforge.testing import TestClient


class _Recorder(Topic):
    id = "rec"
    resumed = False

    async def on_restarted(self):
        self.resumed = True


def test_announce_online_resumes_the_requester(tmp_path):
    async def scenario():
        c = TestClient(home=str(tmp_path))
        (tmp_path / ".restart_announce").write_text("777")
        inst = _Recorder(c.core, 777, "rec", None)
        c.core.instances[777] = inst
        await c.core._announce_online()
        assert any("back online" in text for _tid, text in c.bot.sent)
        assert inst.resumed  # the requester's hook fired

    asyncio.run(scenario())


def test_announce_online_noop_without_pending(tmp_path):
    async def scenario():
        c = TestClient(home=str(tmp_path))
        await c.core._announce_online()
        assert not any("back online" in text for _tid, text in c.bot.sent)

    asyncio.run(scenario())


def test_base_on_restarted_is_a_noop(tmp_path):
    async def scenario():
        c = TestClient(home=str(tmp_path))
        await Topic(c.core, 1, "t", None).on_restarted()  # must not raise

    asyncio.run(scenario())


def test_claude_on_restarted_resubmits(tmp_path):
    async def scenario():
        c = TestClient(Claude(), home=str(tmp_path))
        t = c.core._instantiate(ClaudeTopic, 555, "work")
        prompts = []

        async def _capture(prompt, **kw):
            prompts.append(prompt)

        t.submit = _capture
        await t.on_restarted()
        assert prompts and "continue where you left off" in prompts[0]

    asyncio.run(scenario())
