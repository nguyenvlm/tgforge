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


def test_kill_process_group_kills_what_the_shell_started(tmp_path):
    """A timed-out `!` command must not leave its children running: killing only the
    shell orphaned a child that kept working after the timeout was reported."""
    import os

    from tgforge.base.kernel import kill_process_group

    pid_file = tmp_path / "child.pid"

    async def scenario():
        proc = await asyncio.create_subprocess_shell(
            f"sleep 30 & echo $! > {pid_file}; wait",
            start_new_session=True,
        )
        for _ in range(100):
            if pid_file.exists() and pid_file.read_text().strip():
                break
            await asyncio.sleep(0.02)
        child = int(pid_file.read_text())
        os.kill(child, 0)  # alive before the kill
        await kill_process_group(proc)
        for _ in range(100):
            try:
                os.kill(child, 0)
            except ProcessLookupError:
                return
            with open(f"/proc/{child}/stat") as stat:
                if stat.read().split()[2] == "Z":
                    return
            await asyncio.sleep(0.02)
        raise AssertionError("the shell's child survived the group kill")

    asyncio.run(scenario())
