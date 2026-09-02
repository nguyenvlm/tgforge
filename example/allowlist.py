"""Example: an allowlist auth beyond the default owner-only.

Shows two things the base supports — a custom auth middleware, and a plugin that
manages shared state in its own store. The `Allowlist` plugin adds an admin-only
`/allow` command that grows an allowlist held in the plugin store; the paired
`AllowlistAuth` middleware lets the owner and any allowlisted user through.

Wire it by passing both the plugin and a middleware factory bound to it:

    allow = Allowlist()
    app = App("bot.json", auth=lambda kernel: AllowlistAuth(kernel, allow))
    app.include(allow)
"""

from __future__ import annotations

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, User

from tgforge.base.kernel import Plugin, universal


def _resolve_user(message, args: str) -> int | None:
    """The target user id: a numeric arg, a tap-mention (`text_mention` entity,
    which carries the user), or the replied-to message's sender. A bare `@username`
    mention carries no id and returns None."""
    if args and args.strip().isdigit():
        return int(args.strip())
    for e in getattr(message, "entities", None) or []:
        if getattr(e, "type", None) == "text_mention" and getattr(e, "user", None):
            return e.user.id
    reply = getattr(message, "reply_to_message", None)
    if reply is not None and getattr(reply, "from_user", None):
        return reply.from_user.id
    return None


class Allowlist(Plugin):
    id = "allowlist"

    def allowed(self) -> set[int]:
        return set(self.saved.get("users", []))

    @universal("/allow", "admin: allow another user (tap-mention / reply / user id)")
    async def allow(self, ctx):
        if not ctx.is_admin:
            await ctx.send("only the admin can run /allow")
            return
        target = _resolve_user(ctx.message, ctx.args)
        if target is None:
            await ctx.send(
                "tell me who: reply to their message with /allow, tap-mention them, "
                "or pass a numeric user id"
            )
            return
        users = self.allowed()
        users.add(target)
        self.saved["users"] = sorted(users)
        await ctx.send(f"✅ user {target} is now allowed")


class AllowlistAuth(BaseMiddleware):
    """Front-door check: the owner plus any user on the plugin's allowlist. Passes
    everyone before an owner is bound (so /init can run)."""

    def __init__(self, kernel, plugin: Allowlist):
        self._kernel = kernel
        self._plugin = plugin

    async def __call__(self, handler, event: TelegramObject, data: dict):
        user: User | None = data.get("event_from_user")
        owner = self._kernel.owner_id
        if owner is not None and user is not None:
            if user.id != owner and user.id not in self._plugin.allowed():
                return None
        return await handler(event, data)
