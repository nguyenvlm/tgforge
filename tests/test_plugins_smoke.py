"""Smoke tests for the bundled plugins: registration, the localfs browse window
(open + a directory tap), and the core /restart guard."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from tgforge.base.config import BotConfig
from tgforge.base.kernel import Kernel
from tgforge.plugins.gcloud import Gcloud
from tgforge.plugins.localfs import Localfs, LocalfsTopic
from tgforge.plugins.shell import Shell


class MockBot:
    def __init__(self):
        self._n = 100
        self.sent: list[tuple] = []
        self.edited: list[str] = []

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
        self.edited.append(text)
        return SimpleNamespace(message_id=kw.get("message_id"))

    async def edit_message_reply_markup(self, **kw):
        return SimpleNamespace(message_id=kw.get("message_id"))

    async def send_chat_action(self, **kw):
        return True

    async def delete_message(self, **kw):
        return True


def _kernel(tmp_path):
    cfg = BotConfig(token="x", home=str(tmp_path), owner_id=1, chat_id=100, bot_username="bot")
    return Kernel(MockBot(), cfg, [Shell(), Localfs(), Gcloud()])


def test_registration(tmp_path):
    core = _kernel(tmp_path)
    assert "/gcloud" in core.registry.universals
    assert "/restart" in core.registry.universals  # merged into the core plugin
    assert core.registry.launches["/localfs"][0] == "localfs"
    assert core.registry.launches["/shell"][0] == "shell"


def test_localfs_opens_and_navigates(tmp_path):
    async def scenario():
        base = tmp_path / "base"
        (base / "sub").mkdir(parents=True)
        core = _kernel(tmp_path)
        inst = await core.open_window(100, LocalfsTopic, "browse")
        assert inst.saved["cwd"] == str(tmp_path.resolve())
        # point at a clean dir so "sub" is the only entry, then tap into it
        inst.saved["cwd"] = str(base)
        await inst._redraw()
        cb = SimpleNamespace(
            data="act:fs:cd:0",
            message=SimpleNamespace(message_thread_id=inst.thread_id, chat=SimpleNamespace(id=100)),
        )

        async def _answer():
            return None

        cb.answer = _answer
        await core.handle_callback(cb)
        assert inst.saved["cwd"].endswith("/sub")

    asyncio.run(scenario())


def test_restart_without_service_is_guarded(tmp_path):
    async def scenario():
        core = _kernel(tmp_path)  # no `service` configured
        inst = await core.open_window(100, LocalfsTopic, "browse")
        core.bot.sent.clear()
        await core.handle_message(
            SimpleNamespace(
                text="/restart",
                caption=None,
                photo=None,
                document=None,
                message_id=5,
                from_user=SimpleNamespace(id=1),
                chat=SimpleNamespace(id=100),
                message_thread_id=inst.thread_id,
            )
        )
        assert any("no service configured" in t for _tid, t in core.bot.sent)

    asyncio.run(scenario())
