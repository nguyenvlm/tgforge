"""The Slack adapter: every `slack_sdk` call, event conversion and Slack rule.

A window is a private channel: its key is `slack:<channel id>`; General is the control
channel, created on first start. Calls go through the async Web API client; events,
button taps and the `/tgf` slash command arrive over Socket Mode (an outbound
WebSocket, no public URL), each envelope acknowledged before its handler runs. Text
renders to a Block Kit `markdown` block; `ui.Pages` post the summary in the channel and
each page as a reply in its thread; a `ui.Keyboard` renders as `actions` blocks, the
tap data in each button's `value`. The kernel's `Transport` paces, retries and orders
the calls made here.
"""

from __future__ import annotations

import asyncio
import contextlib
import html
import logging
import re
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

import aiohttp
from slack_sdk.errors import SlackApiError
from slack_sdk.socket_mode.aiohttp import SocketModeClient
from slack_sdk.socket_mode.response import SocketModeResponse
from slack_sdk.web.async_client import AsyncWebClient

from tgforge.base import ui
from tgforge.base.config import BotConfig
from tgforge.base.kernel import (
    Attachment,
    Flood,
    Inbound,
    NotFound,
    NotModified,
    Refused,
    Tap,
    Transient,
)

LOGGER = logging.getLogger("tgforge")

PREFIX = "slack:"
COMMAND = "/tgf"  # the one registered slash command
MARKDOWN_MAX = 12000  # a `markdown` block's text cap
SECTION_MAX = 3000  # a `section` block's plain-text cap
NAME_MAX = 80  # a channel name's cap
_MENTION = re.compile(r"<@([A-Z0-9]+)(?:\|[^>]*)?>")  # a user named in a message
CONTENT_MEMORY = 512  # sent/edited message bodies kept for a buttons-only edit


@dataclass
class SlackMarkdown:
    """A body rendered as a Block Kit `markdown` block (also its notification text)."""

    markdown: str


def make_client(config: BotConfig) -> AsyncWebClient:
    """The Web API client the live bot runs on; retries are the transport's, not the
    client's."""
    return AsyncWebClient(token=config.slack.bot_token, retry_handlers=[])


def channel_slug(title: str) -> str:
    """A channel name for a window title: lowercase letters, digits, `-` and `_`, at
    most NAME_MAX; "window" when nothing is left."""
    slug = re.sub(r"[^a-z0-9_-]+", "-", title.lower())
    slug = re.sub(r"-{2,}", "-", slug).strip("-_")
    return slug[:NAME_MAX].rstrip("-_") or "window"


def clash_name(slug: str, attempt: int) -> str:
    """The `attempt`-th name tried for `slug`: the slug itself, then `-2`, `-3`, …,
    cut so the whole stays within NAME_MAX."""
    if attempt <= 1:
        return slug
    suffix = f"-{attempt}"
    return slug[: NAME_MAX - len(suffix)] + suffix


def _actions(buttons: ui.Keyboard | None) -> list[dict]:
    if buttons is None:
        return []
    return [
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": b.label, "emoji": True},
                    "value": b.data,
                    "action_id": f"tgf:{r}:{i}",
                }
                for i, b in enumerate(row)
            ],
        }
        for r, row in enumerate(buttons.rows)
        if row
    ]


def _plain_sections(text: str) -> list[dict]:
    return [
        {"type": "section", "text": {"type": "plain_text", "text": text[i : i + SECTION_MAX]}}
        for i in range(0, max(len(text), 1), SECTION_MAX)
    ]


def _error(error: SlackApiError) -> str:
    response = error.response
    return (response.get("error") if hasattr(response, "get") else None) or ""


class SlackAdapter:
    """One Slack app in one workspace."""

    platform = "Slack"
    key_prefix = PREFIX
    command_prefix = COMMAND + " "  # what a typed command starts with
    pages_in_thread = True
    renders_tables = False
    max_message = ui.MAX_MSG
    safe_chunk = ui.MAX_MSG  # a render is as long as its source
    photo_max = 1024 * 1024 * 1024  # an image is a file like any other
    upload_max = 1024 * 1024 * 1024
    download_max = 1024 * 1024 * 1024

    def __init__(self, client, config: BotConfig, app_token: str | None = None):
        self.client = client
        self.config = config
        self.app_token = app_token if app_token is not None else config.slack.app_token
        self.bot_user_id: str | None = None
        self.bot_name = ""
        self._contents: OrderedDict[tuple, tuple[str, list]] = OrderedDict()
        self._handlers: set[asyncio.Task] = set()

    @property
    def owner_id(self):
        return self.config.slack.owner_id

    @property
    def general(self) -> str:
        return PREFIX + (self.config.slack.control_channel or "")

    @staticmethod
    def channel(window: str) -> str:
        return window[len(PREFIX) :]

    # ── Rendering ──────────────────────────────────────────────────

    def render(self, markdown: str) -> tuple[SlackMarkdown | None, str]:
        if len(markdown) > MARKDOWN_MAX:
            return None, markdown
        return SlackMarkdown(markdown=markdown), markdown

    def render_pages(self, pages: ui.Pages) -> list[tuple[SlackMarkdown | None, str]]:
        """The summary (and its note) in the channel, then each non-empty page, headed
        `↳ i/n`, as a plain reply in its thread."""
        summary = f"{pages.summary}\n\n{pages.note}" if pages.note else pages.summary
        messages = [self.render(summary)]
        count = len(pages.pages)
        for i, body in enumerate(pages.pages):
            if body:
                messages.append((None, f"↳ {i + 1}/{count}\n\n{body}"))
        return messages

    @staticmethod
    def measure(markdown: str) -> int:
        """A `markdown` block carries the text as written."""
        return len(markdown)

    @staticmethod
    def page_size(head: str, body: str, first: bool) -> int:
        """A thread reply carries the page head and body as written."""
        return len(f"{head}\n\n{body}")

    def classify(self, error: Exception):
        if isinstance(error, SlackApiError):
            response = error.response
            name = _error(error)
            status = getattr(response, "status_code", None)
            if status == 429 or name == "ratelimited":
                headers = getattr(response, "headers", None) or {}
                retry = headers.get("Retry-After") or headers.get("retry-after") or 1
                return Flood(seconds=float(retry))
            if status is not None and status >= 500:
                return Transient()
            if name in ("message_not_found", "channel_not_found"):
                return NotFound()
            if "not_modified" in name:
                return NotModified()
            return Refused()
        if isinstance(error, (aiohttp.ClientError, TimeoutError, ConnectionError)):
            return Transient()
        return None

    # ── Calls ──────────────────────────────────────────────────────

    def _body(self, text, buttons) -> tuple[str, list[dict], dict]:
        """(notification text, content blocks, extra arguments) of a message body."""
        if isinstance(text, SlackMarkdown):
            return text.markdown, [{"type": "markdown", "text": text.markdown}], {}
        if buttons is not None:
            return text, _plain_sections(text), {}
        return text, [], {"mrkdwn": False}

    def _remember(self, channel: str, ts: str, text: str, blocks: list) -> None:
        self._contents[(channel, ts)] = (text, blocks)
        self._contents.move_to_end((channel, ts))
        while len(self._contents) > CONTENT_MEMORY:
            self._contents.popitem(last=False)

    async def send(self, window, text, buttons, reply_to, silent=False, thread=None):
        """Post a message; `reply_to` is not used (a message's place is its order), a
        `thread` posts in that message's thread."""
        channel = self.channel(window)
        fallback, blocks, extra = self._body(text, buttons)
        kw = dict(extra)
        if thread is not None:
            kw["thread_ts"] = thread
        all_blocks = blocks + _actions(buttons)
        if all_blocks:
            kw["blocks"] = all_blocks
        response = await self.client.chat_postMessage(channel=channel, text=fallback, **kw)
        ts = response["ts"]
        self._remember(channel, ts, fallback, blocks)
        return ts

    async def _current(self, channel: str, ts: str) -> tuple[str, list]:
        """A message's text and content blocks (its buttons left out)."""
        known = self._contents.get((channel, ts))
        if known is not None:
            return known
        response = await self.client.conversations_history(
            channel=channel, latest=ts, inclusive=True, limit=1
        )
        messages = response.get("messages") or [{}]
        message = messages[0]
        blocks = [b for b in message.get("blocks") or [] if b.get("type") != "actions"]
        return message.get("text", ""), blocks

    async def edit(self, window, message_id, text, buttons):
        channel = self.channel(window)
        if text is None:
            fallback, blocks = await self._current(channel, message_id)
            extra = {}
        else:
            fallback, blocks, extra = self._body(text, buttons)
        all_blocks = blocks + _actions(buttons)
        await self.client.chat_update(
            channel=channel, ts=message_id, text=fallback, blocks=all_blocks, **extra
        )
        self._remember(channel, message_id, fallback, blocks)
        return True

    async def delete(self, window, message_id):
        await self.client.chat_delete(channel=self.channel(window), ts=message_id)
        self._contents.pop((self.channel(window), message_id), None)
        return True

    @staticmethod
    def _shared_ts(response, channel: str):
        """The message id of an uploaded file's post, when the upload reports it."""
        for file in response.get("files") or [response.get("file") or {}]:
            shares = file.get("shares") or {}
            for scope in ("private", "public"):
                posts = shares.get(scope, {}).get(channel)
                if posts:
                    return posts[0].get("ts")
        return None

    async def send_file(self, window, path: Path, caption):
        channel = self.channel(window)
        kw = {"initial_comment": caption} if caption else {}
        response = await self.client.files_upload_v2(
            channel=channel, file=str(path), filename=path.name, **kw
        )
        return self._shared_ts(response, channel)

    async def send_photo(self, window, data: bytes, caption, buttons=None, name="image.png"):
        channel = self.channel(window)
        kw = {"initial_comment": caption} if caption else {}
        response = await self.client.files_upload_v2(
            channel=channel, content=data, filename=name, **kw
        )
        return self._shared_ts(response, channel)

    async def edit_photo(self, window, message_id, data: bytes, buttons=None, name="image.png"):
        """An uploaded file can't be replaced in place: not landed."""
        return None

    async def download(self, attachment: Attachment, dest: Path) -> Path:
        headers = {"Authorization": f"Bearer {self.config.slack.bot_token}"}
        async with (
            aiohttp.ClientSession() as session,
            session.get(attachment.handle, headers=headers) as response,
        ):
            response.raise_for_status()
            data = await response.read()
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        return dest

    async def _invite_owner(self, channel: str) -> None:
        if self.owner_id is None:
            return
        try:
            await self.client.conversations_invite(channel=channel, users=self.owner_id)
        except SlackApiError as e:
            if _error(e) != "already_in_channel":
                raise

    async def _create_channel(self, title: str) -> str:
        slug = channel_slug(title)
        attempt = 1
        while True:
            try:
                response = await self.client.conversations_create(
                    name=clash_name(slug, attempt), is_private=True
                )
                break
            except SlackApiError as e:
                if _error(e) != "name_taken":
                    raise
                attempt += 1
        channel = response["channel"]["id"]
        await self.client.conversations_setTopic(channel=channel, topic=title)
        await self._invite_owner(channel)
        return channel

    async def create_window(self, title: str) -> str:
        return PREFIX + await self._create_channel(title)

    async def rename_window(self, window, title: str):
        """The slugged title as the channel name (a suffix on a clash), the full title as
        its topic."""
        channel = self.channel(window)
        slug = channel_slug(title)
        attempt = 1
        while True:
            try:
                await self.client.conversations_rename(
                    channel=channel, name=clash_name(slug, attempt)
                )
                break
            except SlackApiError as e:
                if _error(e) != "name_taken":
                    raise
                attempt += 1
        await self.client.conversations_setTopic(channel=channel, topic=title)
        return True

    async def close_window(self, window):
        """Archive the channel: its history stays."""
        await self.client.conversations_archive(channel=self.channel(window))
        return True

    async def window_alive(self, window, title: str | None) -> bool:
        try:
            response = await self.client.conversations_info(channel=self.channel(window))
        except SlackApiError as e:
            if _error(e) == "channel_not_found":
                return False
            raise
        return not response["channel"].get("is_archived", False)

    async def answer_tap(self, tap: Tap):
        """A tap's envelope is acknowledged on arrival; nothing more to answer."""
        return True

    async def identity(self) -> str:
        """Who the bot is, and its control channel (created and saved on first start)."""
        response = await self.client.auth_test()
        self.bot_user_id = response["user_id"]
        self.bot_name = response.get("user") or "tgforge"
        if not self.config.slack.control_channel:
            title = f"{self.config.service or self.bot_name} general"
            self.config.slack.control_channel = await self._create_channel(title)
            self.config.save()
        return self.bot_name

    async def bind_owner(self, user_id) -> None:
        """Bind the owner and invite them to the control channel."""
        self.config.slack.owner_id = user_id
        self.config.save()
        if self.config.slack.control_channel:
            await self._invite_owner(self.config.slack.control_channel)

    # ── Events ─────────────────────────────────────────────────────

    async def to_inbound(self, event: dict) -> Inbound | None:
        """A channel `message` event as an `Inbound`; None for the bot's own posts and
        message edits or deletions. A thread reply carries its parent's text as the
        replied text (none when the parent can't be read)."""
        subtype = event.get("subtype")
        if subtype not in (None, "file_share", "thread_broadcast"):
            return None
        user = event.get("user")
        if event.get("bot_id") or user is None or user == self.bot_user_id:
            return None
        channel = event["channel"]
        text = html.unescape(event.get("text") or "")
        named = [u for u in _MENTION.findall(text) if u != self.bot_user_id]
        mentioned = False
        if self.bot_user_id:
            mention = re.compile(rf"<@{self.bot_user_id}(\|[^>]*)?>")
            mentioned = bool(mention.search(text))
            text = mention.sub("", text)
        ts = event.get("ts")
        thread_ts = event.get("thread_ts")
        inbound = Inbound(
            window=PREFIX + channel,
            message_id=ts,
            user_id=user,
            text=text,
            mentioned=mentioned,
            mentioned_user_ids=list(dict.fromkeys(named)),
            attachments=[
                Attachment(
                    name=f.get("name") or "",
                    kind="photo" if (f.get("mimetype") or "").startswith("image/") else "document",
                    handle=f.get("url_private_download") or f.get("url_private"),
                )
                for f in event.get("files") or []
            ],
        )
        if thread_ts and thread_ts != ts:
            inbound.in_thread = True
            try:
                response = await self.client.conversations_replies(
                    channel=channel, ts=thread_ts, limit=1
                )
            except Exception as e:
                if self.classify(e) is None:
                    raise
                LOGGER.warning("thread parent of %s in %s not read: %r", ts, channel, e)
                return inbound
            parent = (response.get("messages") or [{}])[0]
            inbound.replied_text = html.unescape(parent.get("text") or "").strip()
            if parent.get("bot_id") or parent.get("user") == self.bot_user_id:
                inbound.replied_author = self.bot_name or "the bot"
            else:
                inbound.replied_user_id = parent.get("user")
                inbound.replied_author = f"<@{parent.get('user')}>"
        return inbound

    def command_to_inbound(self, payload: dict) -> Inbound:
        """`/tgf <text>` as the typed command `/<text>` (a bare `/tgf` is `/`, the menu),
        addressed to the bot and echoed into the window."""
        text = (payload.get("text") or "").strip()
        return Inbound(
            window=PREFIX + payload["channel_id"],
            message_id=None,
            user_id=payload.get("user_id"),
            text="/" + text,
            mentioned=True,
            echo=True,
        )

    @staticmethod
    def to_taps(payload: dict) -> list[Tap]:
        """A `block_actions` payload's button taps."""
        if payload.get("type") != "block_actions":
            return []
        channel = (payload.get("channel") or {}).get("id") or (payload.get("container") or {}).get(
            "channel_id"
        )
        message_ts = (payload.get("message") or {}).get("ts") or (
            payload.get("container") or {}
        ).get("message_ts")
        user = (payload.get("user") or {}).get("id")
        return [
            Tap(
                window=PREFIX + channel,
                message_id=message_ts,
                data=action.get("value") or "",
                user_id=user,
                tap_id=payload.get("trigger_id") or "",
            )
            for action in payload.get("actions") or []
        ]

    async def on_request(self, client, request, on_message, on_tap) -> None:
        """Acknowledge a Socket Mode envelope, then handle it in its own task."""
        await client.send_socket_mode_response(SocketModeResponse(envelope_id=request.envelope_id))
        task = asyncio.get_running_loop().create_task(
            self._dispatch(request.type, request.payload, on_message, on_tap)
        )
        self._handlers.add(task)
        task.add_done_callback(self._handlers.discard)

    async def _dispatch(self, kind: str, payload: dict, on_message, on_tap) -> None:
        try:
            if kind == "events_api":
                event = payload.get("event") or {}
                if event.get("type") == "message":
                    inbound = await self.to_inbound(event)
                    if inbound is not None:
                        await on_message(inbound)
            elif kind == "interactive":
                for tap in self.to_taps(payload):
                    await on_tap(tap)
            elif kind == "slash_commands" and payload.get("command") == COMMAND:
                await on_message(self.command_to_inbound(payload))
        except Exception:
            LOGGER.exception("slack %s handler failed", kind)

    async def close(self) -> None:
        """Nothing to close: the Web API client opens a session per call and the Socket
        Mode client closes when `run` ends."""

    async def run(self, on_message, on_tap) -> None:
        """Hold the Socket Mode connection until cancelled."""
        client = SocketModeClient(app_token=self.app_token, web_client=self.client)

        async def listener(socket_client, request):
            await self.on_request(socket_client, request, on_message, on_tap)

        client.socket_mode_request_listeners.append(listener)
        try:
            await client.connect()
            await asyncio.Event().wait()
        finally:
            with contextlib.suppress(Exception):
                await client.close()
