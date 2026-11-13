"""`App.start` installs the stop-signal handlers first, runs kernel startup once, serves
every platform's event source at the same time through the auth hook, keeps serving
when one platform is down (retrying it and reviving its windows once it connects),
exits non-zero when none connects, and on the stop signal drains once, then closes each
platform's client."""

from __future__ import annotations

import asyncio
import os
import signal

import pytest

from tgforge.base import app as app_module
from tgforge.base.app import App, _serve
from tgforge.base.config import BotConfig
from tgforge.base.kernel import AppDB, Inbound, Kernel, Plugin, Topic, Transport, core_ns


class _Adapter:
    """An event source: each `run` hands over its message, then waits; `fail_runs`
    runs raise right after, `identity_failures` connects fail first."""

    platform = "Fake"
    general = "0"
    owner_id = 1

    def __init__(self, key_prefix, message, identity_failures=0, fail_runs=0, seen=None):
        self.key_prefix = key_prefix
        self.message = message
        self.identity_failures = identity_failures
        self.fail_runs = fail_runs
        self.runs = 0
        self.seen = seen if seen is not None else []

    async def identity(self):
        if self.identity_failures:
            self.identity_failures -= 1
            raise ConnectionError("platform down")
        return "bot"

    async def run(self, on_message, on_tap):
        self.runs += 1
        await on_message(self.message)
        if self.fail_runs:
            self.fail_runs -= 1
            raise ConnectionError("dropped")
        await asyncio.Event().wait()

    async def close(self):
        self.seen.append(("close", self.key_prefix))


REVIVED: list[str] = []


class _Window(Topic):
    id = "win"

    async def on_revive(self):
        REVIVED.append(self.window)


class _Windows(Plugin):
    id = "windows"
    topics = [_Window]


@pytest.fixture
def events(monkeypatch):
    seen = []
    REVIVED.clear()

    async def handle_message(self, inbound):
        seen.append(("message", inbound.text))

    real_startup = Kernel.startup

    async def startup(self):
        seen.append("startup")
        return await real_startup(self)

    async def broadcast_shutdown(self):
        seen.append("drain")

    async def no_announce(self):
        return None

    monkeypatch.setattr(Kernel, "handle_message", handle_message)
    monkeypatch.setattr(Kernel, "startup", startup)
    monkeypatch.setattr(Kernel, "broadcast_shutdown", broadcast_shutdown)
    monkeypatch.setattr(Kernel, "_announce_online", no_announce)
    monkeypatch.setattr(app_module, "SOURCE_BACKOFF", 0.01)
    return seen


async def _serve_until(bot, ready):
    """Run `App.start`, send it SIGTERM once `ready()` holds."""

    async def stopper():
        for _ in range(500):
            await asyncio.sleep(0.01)
            if ready():
                break
        os.kill(os.getpid(), signal.SIGTERM)  # the handler `start` installed catches it

    task = asyncio.get_running_loop().create_task(stopper())
    await asyncio.wait_for(bot.start(), 10)
    await task


def _app(tmp_path, monkeypatch, adapters, plugins=()):
    monkeypatch.setattr(
        app_module, "platform_transports", lambda cfg: [Transport(a) for a in adapters]
    )
    bot = App(BotConfig(token="x", home=str(tmp_path)))
    for plugin in plugins:
        bot.include(plugin)
    return bot


def test_start_serves_every_platform_then_drains_and_closes_on_the_stop_signal(
    tmp_path, monkeypatch, events
):
    first = _Adapter("", Inbound("0", 1, 1, "from the first"), seen=events)
    second = _Adapter("x:", Inbound("x:0", 1, 2, "from a stranger"), seen=events)
    bot = _app(tmp_path, monkeypatch, [first, second])
    asyncio.run(_serve_until(bot, lambda: first.runs and second.runs))
    assert events[0] == "startup" and events.count("startup") == 1
    assert events[-3:] == ["drain", ("close", ""), ("close", "x:")]  # closed after the drain
    assert events.count("drain") == 1
    assert ("message", "from the first") in events  # owner 1 passes OwnerOnly
    assert ("message", "from a stranger") not in events  # user 2 is not the owner


def test_no_platform_connecting_exits_non_zero(tmp_path, monkeypatch, events):
    down = _Adapter("", Inbound("0", 1, 1, "never"), identity_failures=1, seen=events)
    bot = _app(tmp_path, monkeypatch, [down])
    with pytest.raises(SystemExit) as stopped:
        asyncio.run(asyncio.wait_for(bot.start(), 5))
    assert stopped.value.code == 1
    assert down.runs == 0 and ("close", "") in events


def test_a_platform_down_at_startup_is_retried_and_its_windows_revived(
    tmp_path, monkeypatch, events
):
    db = AppDB(BotConfig(home=str(tmp_path)).db_file)
    db.saved(core_ns())["owners"] = {"x:5": "win", "7": "win"}
    db.close()
    up = _Adapter("", Inbound("0", 1, 1, "from the live one"), seen=events)
    late = _Adapter("x:", Inbound("x:0", 1, 1, "from the late one"), identity_failures=2)
    bot = _app(tmp_path, monkeypatch, [up, late], plugins=[_Windows()])
    asyncio.run(_serve_until(bot, lambda: late.runs))
    assert late.identity_failures == 0 and late.runs == 1  # two retries, then served
    assert REVIVED == ["7", "x:5"]  # the live platform's at startup, the late one's after
    assert ("message", "from the late one") in events and "drain" in events


def test_a_stop_signal_during_startup_still_drains(tmp_path, monkeypatch, events):
    installed = []

    class _Slow(_Adapter):
        async def identity(self):
            installed.append(signal.getsignal(signal.SIGTERM) is not signal.SIG_DFL)
            if installed[-1]:  # only with a handler: unhandled, it would end the tests
                os.kill(os.getpid(), signal.SIGTERM)
            return "bot"

    slow = _Slow("", Inbound("0", 1, 1, "hi"), seen=events)
    bot = _app(tmp_path, monkeypatch, [slow])
    asyncio.run(asyncio.wait_for(bot.start(), 5))
    assert installed == [True]  # the handler was there before startup ran
    assert "drain" in events


def test_a_source_loop_that_raises_is_restarted_after_a_backoff(tmp_path, monkeypatch, events):
    flaky = _Adapter("", Inbound("0", 1, 1, "hello"), fail_runs=1, seen=events)
    bot = _app(tmp_path, monkeypatch, [flaky])
    asyncio.run(_serve_until(bot, lambda: flaky.runs >= 2))
    assert flaky.runs == 2  # restarted once, then served until the stop signal
    assert events.count(("message", "hello")) == 2 and "drain" in events


def test_the_restart_backoff_doubles_up_to_its_cap(monkeypatch):
    monkeypatch.setattr(app_module, "SOURCE_BACKOFF", 0.02)
    monkeypatch.setattr(app_module, "SOURCE_BACKOFF_MAX", 0.08)
    starts = []

    class _Failing:
        platform = "Fake"

        async def run(self, on_message, on_tap):
            starts.append(asyncio.get_running_loop().time())
            raise ConnectionError("down at once")

    async def scenario():
        task = asyncio.get_running_loop().create_task(_serve(_Failing(), None, None))
        while len(starts) < 7:
            await asyncio.sleep(0.005)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())
    gaps = [b - a for a, b in zip(starts, starts[1:], strict=False)]
    expected = [0.02, 0.04, 0.08, 0.08, 0.08, 0.08]
    assert all(gap >= want * 0.9 for gap, want in zip(gaps, expected, strict=True))
    assert all(gap < want + 0.03 for gap, want in zip(gaps, expected, strict=True))
