"""The `App` object — an aiogram Dispatcher wrapping a shared `BotCore` that the
plugins register into at construction (the registry is built then, so a launch-time
conflict aborts before polling starts).

A passive object the launcher (CLI or `tgforge.run()`) starts. A plugin is a
`Plugin` instance (topic classes + decorated commands); an optional async
`on_startup()` runs once the bot connects. Named `App`, not `Bot`, so it never
collides with `aiogram.Bot` (the transport client).
"""

from __future__ import annotations

from aiogram import BaseMiddleware, Dispatcher, Router
from aiogram import Bot as AioBot
from aiogram.types import CallbackQuery, Message, TelegramObject, User

from tgforge.base.config import BotConfig
from tgforge.base.kernel import Kernel


class OwnerOnly(BaseMiddleware):
    """The default front-door auth: drop every update from anyone but the bound owner,
    before the router sees it. Owner read live from the kernel, so a pre-`/init` update
    (no owner yet) passes and the bootstrap can run. Swap in another middleware via
    `App(config, auth=...)`; see `example/allowlist.py`."""

    def __init__(self, kernel):
        self._kernel = kernel

    async def __call__(self, handler, event: TelegramObject, data: dict):
        user: User | None = data.get("event_from_user")
        owner = self._kernel.owner_id
        if owner is not None and user is not None and user.id != owner:
            return None
        return await handler(event, data)


class App:
    def __init__(self, config: str | BotConfig, auth=None):
        # auth: a middleware factory `kernel -> BaseMiddleware`; defaults to OwnerOnly
        self.config = BotConfig.load(config) if isinstance(config, str) else config
        self.dispatcher = Dispatcher()
        self._plugins: list = []
        self._auth = auth or OwnerOnly

    def include(self, plugin) -> App:
        """Include a Plugin instance."""
        self._plugins.append(plugin)
        return self

    async def start(self) -> None:
        aiobot = AioBot(self.config.token)
        core = Kernel(aiobot, self.config, self._plugins)

        router = Router(name="tgforge")
        auth = self._auth(core)
        router.message.middleware(auth)
        router.callback_query.middleware(auth)

        @router.message()
        async def _on_message(message: Message):
            await core.handle_message(message)

        @router.callback_query()
        async def _on_callback(callback: CallbackQuery):
            await core.handle_callback(callback)

        self.dispatcher.include_router(router)

        async def _boot():
            await core.startup()
            for plugin in core.plugins:
                await plugin.on_startup()

        async def _drain():
            await core.broadcast_shutdown()  # flag + let each window settle its UI

        self.dispatcher.startup.register(_boot)
        self.dispatcher.shutdown.register(_drain)
        await self.dispatcher.start_polling(aiobot)
