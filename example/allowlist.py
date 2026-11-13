"""Example: an allowlist auth beyond the default owner-only.

Shows two things the base supports — a custom auth hook, and a plugin that manages
shared state in its own store. The `Allowlist` plugin adds an admin-only `/allow`
command that grows an allowlist held in the plugin store; the paired `AllowlistAuth`
hook lets the owner of the event's platform and any allowlisted user through.

Wire it by passing both the plugin and the hook bound to it:

    allow = Allowlist()
    app = App("bot.json", auth=AllowlistAuth(allow))
    app.include(allow)
"""

from __future__ import annotations

from tgforge.base.kernel import Plugin, universal


def _resolve_user(message, args: str):
    """The target user id: a numeric arg, the first user the message names (a Telegram
    tap-mention, a Slack @mention), or the replied-to message's author. A bare Telegram
    `@username` carries no id and gives None."""
    arg = (args or "").strip()
    if arg.isdigit():
        return int(arg)
    if message is not None and message.mentioned_user_ids:
        return message.mentioned_user_ids[0]
    if message is not None and message.replied_user_id is not None:
        return message.replied_user_id
    return None


class Allowlist(Plugin):
    id = "allowlist"

    def allowed(self) -> set:
        return set(self.saved.get("users", []))

    @universal("/allow", "admin: allow another user (reply / mention / user id)")
    async def allow(self, ctx):
        if not ctx.is_admin:
            await ctx.send("only the admin can run /allow")
            return
        target = _resolve_user(ctx.message, ctx.args)
        if target is None:
            await ctx.send(
                "tell me who: reply to their message with /allow, mention them, "
                "or pass a numeric user id"
            )
            return
        users = self.allowed()
        users.add(target)
        self.saved["users"] = sorted(users, key=str)
        await ctx.send(f"✅ user {target} is now allowed")


class AllowlistAuth:
    """The auth hook: the owner of the event's platform plus any user on the plugin's
    allowlist. Passes everyone before an owner is bound (so /init can run)."""

    def __init__(self, plugin: Allowlist):
        self._plugin = plugin

    def __call__(self, kernel, event) -> bool:
        owner = kernel.owner_for(event.window)
        if owner is None or event.user_id is None:
            return True
        return event.user_id == owner or event.user_id in self._plugin.allowed()
