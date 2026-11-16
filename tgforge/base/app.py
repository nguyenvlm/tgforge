"""The `App` object — the plugins plus the platforms configured in `bot.json`, served by
one `Kernel` (the registry is built at startup, so a conflict aborts before any
platform connects).

A passive object the launcher (CLI or `tgforge.run()`) starts. A plugin is a `Plugin`
instance (topic classes + decorated commands); an optional async `on_startup()` runs
once the bot connects. Named `App`, not `Bot`, so it never collides with a platform's
client class.
"""

from __future__ import annotations

import asyncio
import contextlib
import gc
import signal
import time

from tgforge.base.config import BotConfig
from tgforge.base.kernel import LOGGER, Kernel, Transport, notice_exception, setup_logging

SLACK_INSTALL = 'uv pip install "tgforge[slack]"'
SOURCE_BACKOFF = 5.0  # s before a stopped platform loop is restarted, doubling
SOURCE_BACKOFF_MAX = 120.0  # s: the longest wait between restarts
FULL_COLLECTION_WARN = 0.1  # s: a full collection this long pauses the event loop


class OwnerOnly:
    """The default auth: drop every event from anyone but the bound owner of the event's
    platform, before the router sees it. The owner is read live, so a pre-`/init` event
    (no owner yet) passes and the bootstrap can run. Pass another `auth(kernel, event)
    -> bool` via `App(config, auth=...)`; see `example/allowlist.py`."""

    def __call__(self, kernel, event) -> bool:
        owner = kernel.owner_for(event.window)
        return owner is None or event.user_id is None or event.user_id == owner


def platform_transports(config: BotConfig) -> list[Transport]:
    """One transport per platform `config` sets: Telegram for `token`, Slack for the
    `slack` block (which needs the `slack` extra)."""
    transports = []
    if config.token:
        from tgforge.base.telegram import TelegramAdapter, make_bot

        transports.append(Transport(TelegramAdapter(make_bot(config.token), config)))
    if config.slack is not None:
        try:
            from tgforge.base.slack import SlackAdapter, make_client
        except ImportError as e:
            raise SystemExit(
                f"bot.json has a `slack` block but Slack support is not installed: {SLACK_INSTALL}"
            ) from e
        transports.append(Transport(SlackAdapter(make_client(config), config)))
    return transports


class App:
    def __init__(self, config: str | BotConfig, auth=None):
        # auth: `auth(kernel, event) -> bool` on each Inbound / Tap; defaults to OwnerOnly
        self.config = BotConfig.load(config) if isinstance(config, str) else config
        self._plugins: list = []
        self._auth = auth or OwnerOnly()

    def include(self, plugin) -> App:
        """Include a Plugin instance."""
        self._plugins.append(plugin)
        return self

    async def start(self) -> None:
        """Start the kernel once and serve every platform's event source until
        SIGTERM/SIGINT, then drain once and close each platform's client. A platform that
        fails to connect is retried in the background while the others serve; when none
        connects the process exits non-zero (the service manager restarts it). A source
        loop that ends is restarted after a backoff."""
        setup_logging()
        transports = platform_transports(self.config)
        kernel = Kernel(self.config, self._plugins, transports)
        kernel.enable_pacing()  # self-limit under each platform's rate (off in tests)

        async def on_message(inbound):
            if self._auth(kernel, inbound):
                await kernel.handle_message(inbound)

        async def on_tap(tap):
            if self._auth(kernel, tap):
                await kernel.handle_tap(tap)

        watch_full_collections = full_collection_watch()
        loop = asyncio.get_running_loop()
        stop = asyncio.Event()
        for sig in (signal.SIGTERM, signal.SIGINT):  # before startup: a stop then still drains
            loop.add_signal_handler(sig, stop.set)
        try:
            failed = await kernel.startup()
            if not kernel.transports:
                LOGGER.error("no platform connected; exiting")
                raise SystemExit(1)
            for plugin in kernel.plugins:
                await plugin.on_startup()
            gc.collect()
            gc.freeze()  # the long-lived startup heap leaves every later collection's scan
            gc.callbacks.append(watch_full_collections)
            sources = [
                loop.create_task(_serve(tx.adapter, on_message, on_tap)) for tx in kernel.transports
            ]
            for tx in failed:

                async def join(name, tx=tx):
                    await kernel.rejoin(tx, name)

                sources.append(loop.create_task(_serve(tx.adapter, on_message, on_tap, join)))
            try:
                await stop.wait()
            finally:
                for task in sources:
                    task.cancel()
                for task in sources:
                    with contextlib.suppress(asyncio.CancelledError):
                        await task
                await kernel.broadcast_shutdown()  # flag + let each window settle its UI
        finally:
            if watch_full_collections in gc.callbacks:
                gc.callbacks.remove(watch_full_collections)
            for tx in transports:
                try:
                    await tx.adapter.close()
                except Exception:
                    LOGGER.exception("closing %s failed", tx.adapter.platform)


def full_collection_watch(clock=time.perf_counter):
    """A `gc.callbacks` hook timing each full (generation 2) collection: one over
    FULL_COLLECTION_WARN is logged at WARNING with its duration and the objects it
    scanned, so a heap grown past the startup freeze shows in the log."""
    started: list[float] = []

    def on_collection(phase: str, info: dict) -> None:
        if info["generation"] != 2:
            return
        if phase == "start":
            started[:] = [clock()]
            return
        if not started:
            return
        took = clock() - started.pop()
        if took > FULL_COLLECTION_WARN:
            LOGGER.warning(
                "full garbage collection took %.0f ms (%d objects scanned, %d frozen at startup)",
                took * 1000,
                len(gc.get_objects(generation=2)),
                gc.get_freeze_count(),
            )

    return on_collection


async def _serve(adapter, on_message, on_tap, join=None) -> None:
    """Run one platform's event source for good. With `join` (the platform failed to
    connect at startup), first retry its `identity()` with the same backoff and hand the
    name to `join`. A loop that ends (an error or a return) is logged and restarted
    after SOURCE_BACKOFF, doubling while it keeps ending quickly, up to
    SOURCE_BACKOFF_MAX. Each failure is raised as an exception notice in the platform's
    General (its `on_notice`, set by its transport)."""
    loop = asyncio.get_running_loop()
    backoff = SOURCE_BACKOFF
    if join is not None:
        while True:
            try:
                name = await adapter.identity()
                break
            except Exception as error:
                LOGGER.exception(
                    "%s still not connected; retrying in %.0fs", adapter.platform, backoff
                )
                notice_exception(adapter.on_notice, adapter.general, "platform connect", error)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, SOURCE_BACKOFF_MAX)
        await join(name)
        backoff = SOURCE_BACKOFF
    while True:
        started = loop.time()
        try:
            await adapter.run(on_message, on_tap)
            LOGGER.warning("%s loop ended; restarting in %.0fs", adapter.platform, backoff)
        except Exception as error:
            LOGGER.exception("%s loop failed; restarting in %.0fs", adapter.platform, backoff)
            notice_exception(adapter.on_notice, adapter.general, "platform loop", error)
        if loop.time() - started > SOURCE_BACKOFF_MAX:
            backoff = SOURCE_BACKOFF  # it had been serving: start the backoff over
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, SOURCE_BACKOFF_MAX)
