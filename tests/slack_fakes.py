"""A fake Slack Web API client for the Slack-specific tests: it records every call and
answers like the Web API, so the real `SlackAdapter` runs on top of it with no network."""

from __future__ import annotations

from slack_sdk.errors import SlackApiError

from tgforge.base.config import BotConfig, SlackConfig
from tgforge.base.kernel import Kernel, Transport
from tgforge.base.slack import SlackAdapter
from tgforge.testing import MockBot, telegram_transport


class FakeResponse(dict):
    """A Web API response body with its HTTP status and headers."""

    def __init__(self, body: dict, status_code: int = 200, headers: dict | None = None):
        super().__init__(body)
        self.status_code = status_code
        self.headers = headers or {}


def api_error(error: str, status_code: int = 200, headers: dict | None = None) -> SlackApiError:
    return SlackApiError(error, FakeResponse({"ok": False, "error": error}, status_code, headers))


class FakeSlackClient:
    """Records `(method, kwargs)` per call; `fail[method]` lists errors raised by the next
    calls of that method; `parents[ts]` is a thread parent `conversations_replies` returns."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.fail: dict[str, list[Exception]] = {}
        self.parents: dict[str, dict] = {}
        self._ts = 1700000000
        self._channels = 0

    def _hit(self, method: str, kw: dict) -> None:
        self.calls.append((method, kw))
        pending = self.fail.get(method)
        if pending:
            raise pending.pop(0)

    def _next_ts(self) -> str:
        self._ts += 1
        return f"{self._ts}.000100"

    def of(self, method: str) -> list[dict]:
        return [kw for m, kw in self.calls if m == method]

    async def auth_test(self, **kw):
        self._hit("auth_test", kw)
        return FakeResponse({"ok": True, "user_id": "B0", "user": "mybot"})

    async def chat_postMessage(self, **kw):
        self._hit("chat_postMessage", kw)
        return FakeResponse({"ok": True, "ts": self._next_ts(), "channel": kw["channel"]})

    async def chat_update(self, **kw):
        self._hit("chat_update", kw)
        return FakeResponse({"ok": True, "ts": kw["ts"]})

    async def chat_delete(self, **kw):
        self._hit("chat_delete", kw)
        return FakeResponse({"ok": True})

    async def conversations_create(self, **kw):
        self._hit("conversations_create", kw)
        self._channels += 1
        return FakeResponse({"ok": True, "channel": {"id": f"C{self._channels}"}})

    async def conversations_setTopic(self, **kw):
        self._hit("conversations_setTopic", kw)
        return FakeResponse({"ok": True})

    async def conversations_invite(self, **kw):
        self._hit("conversations_invite", kw)
        return FakeResponse({"ok": True})

    async def conversations_rename(self, **kw):
        self._hit("conversations_rename", kw)
        return FakeResponse({"ok": True})

    async def conversations_archive(self, **kw):
        self._hit("conversations_archive", kw)
        return FakeResponse({"ok": True})

    async def conversations_info(self, **kw):
        self._hit("conversations_info", kw)
        return FakeResponse({"ok": True, "channel": {"id": kw["channel"], "is_archived": False}})

    async def conversations_history(self, **kw):
        self._hit("conversations_history", kw)
        return FakeResponse({"ok": True, "messages": [{"text": "older", "blocks": []}]})

    async def conversations_replies(self, **kw):
        self._hit("conversations_replies", kw)
        return FakeResponse({"ok": True, "messages": [self.parents.get(kw["ts"], {})]})

    async def files_upload_v2(self, **kw):
        self._hit("files_upload_v2", kw)
        return FakeResponse({"ok": True, "files": [{"id": "F1"}]})


def slack_config(home: str, **slack) -> BotConfig:
    """Both platforms configured: Telegram owner 1 in chat 100, Slack owner U1 with
    control channel C0."""
    fields = {"bot_token": "xoxb-x", "app_token": "xapp-x", "owner_id": "U1"}
    fields["control_channel"] = "C0"
    fields.update(slack)
    return BotConfig(
        token="x",
        home=home,
        owner_id=1,
        chat_id=100,
        bot_username="bot",
        slack=SlackConfig(**fields),
    )


def slack_adapter(config: BotConfig, client: FakeSlackClient | None = None) -> SlackAdapter:
    adapter = SlackAdapter(client or FakeSlackClient(), config)
    adapter.bot_user_id = "B0"
    adapter.bot_name = "mybot"
    return adapter


def two_platform_kernel(config: BotConfig, plugins: list) -> Kernel:
    """A kernel serving Telegram (over a `MockBot`) and Slack (over a `FakeSlackClient`)."""
    telegram = telegram_transport(MockBot(), config)
    telegram.bot_name = "bot"
    slack = Transport(slack_adapter(config))
    slack.bot_name = "mybot"
    return Kernel(config, plugins, [telegram, slack])


def slack_client(kernel: Kernel) -> FakeSlackClient:
    return kernel.transports[1].adapter.client
