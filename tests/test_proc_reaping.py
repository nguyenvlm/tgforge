"""The catch-all: any subprocess a window spawns through `self.spawn` is reaped by the
kernel when the window closes — even if the window's own `on_close` never reaps it. This
is what makes a leaked child structurally impossible, so individual kill sites are just
signals."""

from __future__ import annotations

import asyncio

from tgforge.base.kernel import Topic
from tgforge.testing import TestClient


class _Spawner(Topic):
    id = "spawner"

    async def on_open(self):
        self.proc = await self.spawn("/bin/sleep", "30")  # long-lived

    async def on_close(self):
        pass  # deliberately does NOT reap — the kernel sweep must


def test_close_reaps_a_child_the_window_ignores(tmp_path):
    async def scenario():
        c = TestClient(home=str(tmp_path))
        inst = await c.core.open_window(100, _Spawner, "s")
        proc = inst.proc
        assert proc.returncode is None  # still running before close
        await c.core.close_window(inst.thread_id)
        assert proc.returncode is not None  # kernel reaped it on close

    asyncio.run(scenario())


def test_release_also_reaps(tmp_path):
    async def scenario():
        c = TestClient(home=str(tmp_path))
        inst = await c.core.open_window(100, _Spawner, "s")
        proc = inst.proc
        await c.core.release_window(inst.thread_id)  # window → core, same teardown
        assert proc.returncode is not None

    asyncio.run(scenario())
