"""In-process test harness: drive a bot with plain calls, read what it sent.

`TestClient` wraps a live Kernel over a `MockBot` (a recording aiogram stand-in),
so tests exercise routing/menus/prompts with no real Telegram and no network.
Blocking prompts (a picker, the menu, a Back loop) run as a background task so a
test can `tap` the button that unblocks them.

    bot = TestClient(Shell(), Localfs())
    await bot.send("@bot /shell")          # open a window in General
    await bot.send("ls", thread_id=bot.window)
    await bot.tap("🖥 Shell")              # tap a menu button by its label
    assert "pong" in bot.replies

Every method is a coroutine (drive it under `asyncio.run`). `owner`/`chat` are
bound by default so `/init` isn't needed; pass `owner_id=None` to test bootstrap.
"""

from __future__ import annotations

import asyncio
import tempfile
from types import SimpleNamespace

from tgforge.base.config import BotConfig
from tgforge.base.kernel import Kernel
from tgforge.base.ui import blabel


class MockBot:
    """A stand-in for `aiogram.Bot` that records instead of calling Telegram."""

    def __init__(self):
        self._next = 1000
        self.sent: list[tuple[int | None, str]] = []  # (thread_id, text)
        self.edits: list[tuple[int, str]] = []  # (message_id, text)
        self.markup_cleared: list[int] = []
        self.deleted_topics: list[int] = []
        self.last_markup = None

    def _id(self) -> int:
        self._next += 1
        return self._next

    async def get_me(self):
        return SimpleNamespace(username="bot")

    async def create_forum_topic(self, chat_id, name):
        return SimpleNamespace(message_thread_id=self._id())

    async def delete_forum_topic(self, chat_id, message_thread_id):
        self.deleted_topics.append(message_thread_id)
        return True

    async def edit_forum_topic(self, chat_id, message_thread_id, name):
        return True

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((kw.get("message_thread_id"), text))
        if kw.get("reply_markup") is not None:
            self.last_markup = kw["reply_markup"]
        return SimpleNamespace(message_id=self._id())

    async def send_photo(self, chat_id, photo, **kw):
        self.sent.append((kw.get("message_thread_id"), "<photo>"))
        if kw.get("reply_markup") is not None:
            self.last_markup = kw["reply_markup"]
        return SimpleNamespace(message_id=self._id())

    async def edit_message_media(self, media, **kw):
        self.edits.append((kw.get("message_id"), "<photo>"))
        if kw.get("reply_markup") is not None:
            self.last_markup = kw["reply_markup"]
        return SimpleNamespace(message_id=kw.get("message_id"))

    async def edit_message_text(self, text, **kw):
        self.edits.append((kw.get("message_id"), text))
        if kw.get("reply_markup") is not None:
            self.last_markup = kw["reply_markup"]
        return SimpleNamespace(message_id=kw.get("message_id"))

    async def edit_message_reply_markup(self, **kw):
        self.markup_cleared.append(kw.get("message_id"))
        return SimpleNamespace(message_id=kw.get("message_id"))

    async def send_chat_action(self, **kw):
        return True

    async def delete_message(self, chat_id, message_id):
        return True


class TestClient:
    __test__ = False  # a test harness, not a pytest test case

    def __init__(
        self,
        *plugins,
        owner_id: int | None = 1,
        chat_id: int = 100,
        username: str = "bot",
        home: str | None = None,
    ):
        self.bot = MockBot()
        cfg = BotConfig(
            token="x",
            home=home or tempfile.mkdtemp(prefix="tgforge-test-"),
            owner_id=owner_id,
            chat_id=chat_id,
            bot_username=username,
        )
        self.core = Kernel(self.bot, cfg, list(plugins))
        self.core.bot_username = username
        self._chat = chat_id
        self._user = owner_id if owner_id is not None else 1
        self._pending: list[asyncio.Task] = []

    # ── Driving ────────────────────────────────────────────────────
    def _msg(self, text, thread_id):
        return SimpleNamespace(
            text=text,
            caption=None,
            photo=None,
            document=None,
            from_user=SimpleNamespace(id=self._user),
            chat=SimpleNamespace(id=self._chat),
            message_thread_id=thread_id,
        )

    async def send(self, text: str, thread_id: int | None = None) -> TestClient:
        """Deliver a message. A message that opens a blocking prompt runs in the
        background so the test can `tap` to unblock it; a plain command completes
        before this returns."""
        task = asyncio.create_task(self.core.handle_message(self._msg(text, thread_id)))
        self._pending.append(task)
        await self._breathe()
        return self

    async def tap(self, label: str) -> TestClient:
        """Tap the button whose text matches `label` on the most recent keyboard."""
        for _ in range(200):
            await asyncio.sleep(0)
            data = self._find_button(label)
            if data is not None:
                self.bot.last_markup = None
                cb = SimpleNamespace(data=data, message=None, answer=self._answer)
                await self.core.handle_callback(cb)
                await self._breathe()
                return self
        raise AssertionError(f"button {label!r} never rendered")

    async def settle(self) -> TestClient:
        """Await any still-running background message tasks. Do not call this while an
        interactive menu is still open (it loops awaiting taps and never completes) —
        use `pump` to let queued work run instead."""
        if self._pending:
            await asyncio.gather(*self._pending, return_exceptions=True)
            self._pending.clear()
        return self

    async def pump(self, n: int = 30) -> TestClient:
        """Yield the event loop `n` times so queued work (a reply, a menu re-render)
        runs — without awaiting a still-open menu task the way `settle` would."""
        for _ in range(n):
            await asyncio.sleep(0)
        return self

    async def _breathe(self) -> None:
        for _ in range(5):
            await asyncio.sleep(0)

    async def _answer(self, *a, **k):
        return None

    def _find_button(self, label: str) -> str | None:
        kb = self.bot.last_markup
        if kb is None:
            return None
        for row in kb.inline_keyboard:
            for btn in row:
                if btn.text == blabel(label):
                    return btn.callback_data
        return None

    # ── Inspecting ─────────────────────────────────────────────────
    @property
    def replies(self) -> list[str]:
        return [text for _tid, text in self.bot.sent]

    @property
    def edits(self) -> list[str]:
        return [text for _mid, text in self.bot.edits]

    @property
    def buttons(self) -> list[str]:
        kb = self.bot.last_markup
        if kb is None:
            return []
        return [btn.text for row in kb.inline_keyboard for btn in row]

    @property
    def window(self) -> int | None:
        """The most recently opened window's thread id (None if none)."""
        return next(iter(reversed(list(self.core.owners))), None)
