"""Authorization: the default OwnerOnly hook serves only the owner; the example
allowlist (a custom auth hook + an admin /allow command managing state in its own
plugin store) extends that to added users with no restart."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from example.allowlist import Allowlist, AllowlistAuth
from tgforge.base.app import OwnerOnly
from tgforge.base.config import BotConfig
from tgforge.base.kernel import Inbound
from tgforge.testing import kernel_bot, telegram_inbound, telegram_kernel


class MockBot:
    def __init__(self):
        self._n = 100
        self.sent: list[tuple] = []

    async def get_me(self):
        return SimpleNamespace(username="bot")

    async def create_forum_topic(self, chat_id, name):
        self._n += 1
        return SimpleNamespace(message_thread_id=self._n)

    async def delete_forum_topic(self, **kw):
        return True

    async def edit_forum_topic(self, **kw):
        return True

    async def send_message(self, chat_id, text, **kw):
        self._n += 1
        self.sent.append((kw.get("message_thread_id"), text))
        return SimpleNamespace(message_id=self._n)

    async def edit_message_text(self, text, **kw):
        return SimpleNamespace(message_id=kw.get("message_id"))

    async def edit_message_reply_markup(self, **kw):
        return SimpleNamespace(message_id=kw.get("message_id"))

    async def send_chat_action(self, **kw):
        return True

    async def delete_message(self, **kw):
        return True


def _kernel(tmp_path, plugins=None, **cfg):
    conf = BotConfig(token="x", home=str(tmp_path), chat_id=100, bot_username="bot", **cfg)
    return telegram_kernel(MockBot(), conf, plugins or [])


def _msg(text, window, uid=1, reply_from=None):
    reply = SimpleNamespace(from_user=SimpleNamespace(id=reply_from)) if reply_from else None
    return telegram_inbound(
        text, window, user_id=uid, message_id=9, reply_to_message=reply, entities=[]
    )


def _drops(auth, core, uid, window="0"):
    return not auth(core, Inbound(window=window, message_id=None, user_id=uid, text="x"))


def test_owner_only_default(tmp_path):
    core = _kernel(tmp_path, owner_id=1)
    auth = OwnerOnly()
    assert _drops(auth, core, 2)  # stranger dropped
    assert not _drops(auth, core, 1)  # owner passes
    core.config.owner_id = None
    assert not _drops(auth, core, 999)  # before /init, anyone passes


def test_example_allowlist(tmp_path):
    async def scenario():
        allow = Allowlist()
        core = _kernel(tmp_path, [allow], owner_id=1)
        auth = AllowlistAuth(allow)
        assert _drops(auth, core, 2)  # not yet allowed

        # admin opens a window and allows user 2 by replying to their message
        await core.handle_message(_msg("@bot /new", None))
        tid = next(t for t in core.owners)
        await core.handle_message(_msg("/allow", tid, uid=1, reply_from=2))
        assert 2 in allow.allowed()
        assert not _drops(auth, core, 2)  # now passes, no restart
        assert _drops(auth, core, 3)  # still-unknown user dropped

    asyncio.run(scenario())


def test_allow_is_admin_only(tmp_path):
    async def scenario():
        allow = Allowlist()
        core = _kernel(tmp_path, [allow], owner_id=1)
        await core.handle_message(_msg("@bot /new", None))
        tid = next(t for t in core.owners)
        kernel_bot(core).sent.clear()
        await core.handle_message(_msg("/allow", tid, uid=7, reply_from=2))
        assert any("only the admin" in t for _t, t in kernel_bot(core).sent)
        assert 2 not in allow.allowed()

    asyncio.run(scenario())


def test_allowlist_persists(tmp_path):
    allow = Allowlist()
    core = _kernel(tmp_path, [allow], owner_id=1)  # noqa: F841 — assigns plugin.saved
    allow.saved["users"] = [42]
    allow2 = Allowlist()
    _kernel(tmp_path, [allow2], owner_id=1)
    assert 42 in allow2.allowed()


def test_allow_by_telegram_tap_mention(tmp_path):
    async def scenario():
        allow = Allowlist()
        core = _kernel(tmp_path, [allow], owner_id=1)
        core.transports[0].adapter.bot_id = 999
        await core.handle_message(_msg("@bot /new", None))
        tid = next(t for t in core.owners)
        entities = [
            SimpleNamespace(type="text_mention", user=SimpleNamespace(id=999)),  # the bot
            SimpleNamespace(type="text_mention", user=SimpleNamespace(id=5)),
        ]
        message = SimpleNamespace(
            message_id=9,
            text="/allow Alice",
            from_user=SimpleNamespace(id=1),
            chat=SimpleNamespace(id=100),
            message_thread_id=int(tid),
            entities=entities,
        )
        await core.handle_message(core.transports[0].adapter.to_inbound(message))
        assert allow.allowed() == {5}

    asyncio.run(scenario())
