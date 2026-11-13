"""The Telegram adapter: every aiogram call, update conversion and Telegram rule.

A window is a forum topic: its key is the thread id as a decimal string, General is
"0". Text renders to MarkdownV2 through `to_md` (telegramify-markdown with the layout
kept); `ui.Pages` render as one message per page (the summary above a collapsed block,
then `↳ i/n` blocks), each measured as that MarkdownV2 page; a `ui.Keyboard` renders as
an inline keyboard. The kernel's `Transport` paces, retries and orders the
calls made here.
"""

from __future__ import annotations

import copy
import re
from pathlib import Path

from aiogram import Bot as AioBot
from aiogram import Dispatcher, Router
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    Message,
    ReplyParameters,
)
from telegramify_markdown import entities_to_markdownv2
from telegramify_markdown.config import get_runtime_config
from telegramify_markdown.converter import STANDARD_OPTIONS, EventWalker, _parse

from tgforge.base import ui
from tgforge.base.config import BotConfig
from tgforge.base.kernel import (
    REQUEST_TIMEOUT,
    Attachment,
    Flood,
    Inbound,
    NotFound,
    NotModified,
    Refused,
    Tap,
    Transient,
)
from tgforge.base.ui import MAX_MSG

SAFE_RAW_CHUNK = 1800  # raw size whose MarkdownV2 conversion always fits MAX_MSG


# ── MarkdownV2 ─────────────────────────────────────────────────────

_SPOILER_TAGS = ("<tg-spoiler>", "</tg-spoiler>")
# bullet symbol per list depth, repeating past the last one
BULLET_SYMBOLS = ("◆", "▸", "⦁")
# spaces per nesting level; the library writes 2 of them itself
LIST_LEVEL_INDENT = 4


class LayoutKeepingWalker(EventWalker):
    """telegramify-markdown's event walker, changed to keep the source layout:
    raw HTML stays as literal text; a list item's later lines and blocks sit right
    under its bullet, first line indented, and an item with more than one block is
    set off from what follows it in the list by a blank line, so each bullet reads as one group.

    Nesting levels are LIST_LEVEL_INDENT spaces apart. A nested item's source indent
    is at least 2 per level and a hanging indent is capped at twice the source line's
    own, so leading whitespace stays within the 2x growth `md_chunks` relies on;
    bullets nested on one source line (`- - a`) have no source indent and exceed it."""

    def __init__(self, config, source_markdown: str) -> None:
        # own copy: the bullet symbol is set per item, the runtime config is shared
        super().__init__(copy.deepcopy(config), source_markdown)
        self._event_range: dict[str, int] | None = None
        self._in_html_block = False
        self._html_block_lines = 0
        self._extra_indent = ""
        # per open list item: [output offset where its content starts, hanging indent
        # width, whether it has a later block]
        self._item_stack: list[list] = []
        # a closed item had a later block: the next bullet or block inside the list
        # starts after a blank line
        self._gap_pending = False

    def _handle_event(self, event) -> None:
        payload, source_range = event if isinstance(event, tuple) else (event, None)
        self._event_range = source_range
        if isinstance(payload, dict):
            if payload.get("Start") == "HtmlBlock":
                self._start_block(self._source_start(source_range))
                self._in_html_block = True
                self._html_block_lines = 0
                return
            if payload.get("End") == "HtmlBlock":
                self._in_html_block = False
                if not self._list_stack:
                    self._mark_block_end()
                return
            if "Html" in payload and self._in_html_block:
                self._on_html_block_line(payload["Html"])
                return
        super()._handle_event(event)

    def _on_html_block_line(self, line: str) -> None:
        if self._html_block_lines:
            self._buf.write("\n" + self._hanging_indent(self._source_start(self._event_range)))
        self._html_block_lines += 1
        self._on_text(line.rstrip("\n"))

    def _on_inline_html(self, html: str) -> None:
        if html.strip().lower() in _SPOILER_TAGS:
            super()._on_inline_html(html)
        else:
            self._on_text(html)

    def _on_start_item(self) -> None:
        ordered = self._list_stack[-1] if self._list_stack else None
        depth = len(self._list_stack)
        self._config.markdown_symbol.unordered_list_item = BULLET_SYMBOLS[
            (depth - 1) % len(BULLET_SYMBOLS)
        ]
        self._write_newlines(2 if self._gap_pending else 1)
        self._gap_pending = False
        super()._on_start_item()
        # put in front of the library's bullet write as a write of its own, so a task
        # marker (which replaces the library's last write) keeps it
        self._extra_indent = " " * ((LIST_LEVEL_INDENT - 2) * (depth - 1))
        if self._extra_indent:
            bullet = self._buf.pop_last()
            self._buf.write(self._extra_indent)
            self._buf.write(bullet)
        marker = (
            f"{ordered}{self._config.markdown_symbol.ordered_list_suffix} "
            if ordered is not None
            else f"{self._config.markdown_symbol.unordered_list_item} "
        )
        width = len(self._extra_indent + self._item_indent + marker)
        self._item_stack.append([self._buf.py_offset, width, False])

    def _on_task_list_marker(self, checked: bool) -> None:
        super()._on_task_list_marker(checked)
        symbol = self._config.markdown_symbol
        mark = symbol.task_completed if checked else symbol.task_uncompleted
        width = len(f"{self._extra_indent}{self._item_indent}{mark} ")
        self._item_stack[-1][:2] = [self._buf.py_offset, width]

    def _on_end_item(self) -> None:
        super()._on_end_item()
        if self._item_stack and self._item_stack.pop()[2]:
            self._gap_pending = True

    def _on_end_list(self) -> None:
        super()._on_end_list()
        if not self._list_stack:
            self._gap_pending = False

    def _write_newlines(self, count: int) -> None:
        """End the output with at least `count` newlines (none at the very start)."""
        needed = count - self._buf.trailing_newline_count()
        if self._buf.py_offset > 0 and needed > 0:
            self._buf.write("\n" * needed)

    def _source_line_indent(self, position: int | None, forward: bool) -> int:
        """Spaces/tabs before `position` on its source line (0 when other text
        precedes it), or after it when `forward` (`position` is a line start)."""
        if position is None:
            return 0
        source = self._source_bytes
        count = 0
        if forward:
            while source[position + count : position + count + 1] in (b" ", b"\t"):
                count += 1
            return count
        i = position - 1
        while i >= 0 and source[i : i + 1] in (b" ", b"\t"):
            count += 1
            i -= 1
        return count if i < 0 or source[i : i + 1] in (b"\n", b"\r") else 0

    def _hanging_indent(self, source_start: int | None, forward: bool = False) -> str:
        if (
            not self._list_stack
            or not self._item_stack
            or self._in_table_cell
            or self._blockquote_scopes  # Telegram's quote has no indent
        ):
            return ""
        width = self._item_stack[-1][1]
        return " " * min(width, 2 * self._source_line_indent(source_start, forward))

    def _on_soft_break(self) -> None:
        super()._on_soft_break()
        if not self._in_code_block:
            end = self._event_range.get("end") if self._event_range else None
            self._buf.write(self._hanging_indent(end, forward=True))

    def _on_hard_break(self) -> None:
        super()._on_hard_break()
        if not self._in_code_block:
            end = self._event_range.get("end") if self._event_range else None
            self._buf.write(self._hanging_indent(end, forward=True))

    def _start_block(self, source_start: int | None, indent: bool = True) -> None:
        """Open a block: a later block of a list item goes on the next line, indented
        under its bullet unless `indent` is off; any other block gets the library's
        block spacing."""
        if not self._list_stack:
            self._ensure_block_spacing(source_start)
            return
        if not self._item_stack or self._buf.py_offset <= self._item_stack[-1][0]:
            return
        self._item_stack[-1][2] = True
        self._write_newlines(2 if self._gap_pending else 1)
        self._gap_pending = False
        if indent:
            self._buf.write(self._hanging_indent(source_start))

    def _on_start_paragraph(self, source_start: int | None = None) -> None:
        if self._list_stack:
            self._start_block(source_start)
        else:
            super()._on_start_paragraph(source_start)

    def _on_end_code_block(self) -> None:
        if self._list_stack:
            self._start_block(self._code_block_start_source, indent=False)
        super()._on_end_code_block()


def to_md(text: str) -> str:
    """GFM → Telegram MarkdownV2, the library's `markdownify` pipeline run through
    `LayoutKeepingWalker`. Returns the text unchanged on any converter error; the
    send path then retries the message plain (no parse_mode)."""
    try:
        parsed, events = _parse(text, STANDARD_OPTIONS, latex_escape=True)
        plain, entities, _ = LayoutKeepingWalker(get_runtime_config(), parsed).walk(events)
        return entities_to_markdownv2(plain, entities)
    except Exception:
        return text


def _escape(text: str) -> str:
    return "".join("\\" + c if c in "_*[]()~`>#+-=|{}.!\\" else c for c in text)


def _collapsed(lines: list[str]) -> str:
    rows = [("**>" if i == 0 else ">") + _escape(ln) for i, ln in enumerate(lines)]
    return "\n".join(rows) + "||"


def _expandable(header: str, body: str) -> str:
    """A collapsed-by-default MarkdownV2 expandable blockquote: `header` is the
    always-shown first line, `body` the hidden-until-tapped remainder."""
    return _collapsed([header] + (["", *body.split("\n")] if body else []))


def _page(head: str, body: str, first: bool) -> tuple[str, str]:
    """(MarkdownV2, plain) of one page: page 1 is `head` as a plain line with `body`
    collapsed under it (no block when empty); a later page is one expandable headed `head`."""
    if not first:
        return _expandable(head, body), head + (f"\n\n{body}" if body else "")
    if not body:
        return _escape(head), head
    quote = _collapsed(body.split("\n"))
    return f"{_escape(head)}\n{quote}", f"{head}\n{body}"


def pages_md(pages: ui.Pages) -> list[tuple[str, str]]:
    """(MarkdownV2, plain) per page: page 1 is the summary as a plain line above its
    collapsed body, then the note a blank line below; each later page one collapsed block
    headed `↳ i/n`."""
    out = [_page(ui.page_head(pages, i), body, i == 0) for i, body in enumerate(pages.pages)]
    if pages.note and out:
        md, plain = out[0]
        out[0] = (f"{md}\n\n{_escape(pages.note)}", f"{plain}\n\n{pages.note}")
    return out


GENERAL = "0"  # General (outside any forum topic) has no thread id
_COMMAND_TARGET = re.compile(r"^(\s*/[^\s@]*)@\S*")  # the `@botname` of `/cmd@botname`


class MarkdownV2(str):
    """A body already rendered to MarkdownV2 (sent with that parse mode)."""


def make_bot(token: str) -> AioBot:
    """The aiogram client the live bot runs on."""
    return AioBot(token, session=AiohttpSession(timeout=REQUEST_TIMEOUT))


def _markup(buttons: ui.Keyboard | None) -> InlineKeyboardMarkup | None:
    if buttons is None:
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=b.label, callback_data=b.data) for b in row]
            for row in buttons.rows
        ]
    )


def _attachments(message) -> list[Attachment]:
    if message is None:
        return []
    out = []
    photo = getattr(message, "photo", None)
    if photo:
        out.append(Attachment(name="", kind="photo", handle=photo[-1].file_id))
    document = getattr(message, "document", None)
    if document:
        name = Path(document.file_name).name if document.file_name else ""
        out.append(Attachment(name=name, kind="document", handle=document.file_id))
    return out


def _mentioned_user_ids(message, bot_id) -> list[int]:
    """The users a message names by a tap-mention (`text_mention`, the one mention that
    carries the user), the bot left out."""
    entities = getattr(message, "entities", None) or getattr(message, "caption_entities", None)
    out = []
    for entity in entities or []:
        user = getattr(entity, "user", None)
        if getattr(entity, "type", None) == "text_mention" and user is not None:
            if user.id != bot_id and user.id not in out:
                out.append(user.id)
    return out


def _forward_source(message) -> str | None:
    origin = getattr(message, "forward_origin", None)
    if origin is None:
        return None
    user = getattr(origin, "sender_user", None)
    chat = getattr(origin, "sender_chat", None) or getattr(origin, "chat", None)
    return (
        getattr(user, "full_name", None)
        or getattr(origin, "sender_user_name", None)
        or getattr(chat, "title", None)
        or "an unknown source"
    )


class TelegramAdapter:
    """One Telegram bot in one forum group (`config.chat_id`)."""

    platform = "Telegram"
    key_prefix = ""
    command_prefix = "/"  # what a typed command starts with
    general = GENERAL
    pages_in_thread = False
    renders_tables = False  # a Markdown table arrives as raw text
    max_message = MAX_MSG
    photo_max = 10 * 1024 * 1024
    upload_max = 50 * 1024 * 1024
    download_max = 20 * 1024 * 1024  # the bot API's getFile cap
    safe_chunk = SAFE_RAW_CHUNK

    def __init__(self, bot, config: BotConfig):
        self.bot = bot
        self.config = config
        self._init_chats: dict[int, int] = {}  # before /init: user id → the chat they wrote in
        self.bot_id: int | None = None  # learned by `identity()`

    @property
    def owner_id(self):
        return self.config.owner_id

    @property
    def bot_name(self) -> str:
        """The bot's username, as `identity()` saved it."""
        return self.config.bot_username

    @property
    def _chat(self):
        return self.config.chat_id

    @staticmethod
    def _thread(window: str) -> int | None:
        return None if window == GENERAL else int(window)

    @staticmethod
    def window_of(thread_id: int | None) -> str:
        return GENERAL if thread_id is None else str(thread_id)

    def _where(self, window: str) -> dict:
        thread = self._thread(window)
        return {"message_thread_id": thread} if thread is not None else {}

    # ── Rendering ──────────────────────────────────────────────────

    def render(self, markdown: str) -> tuple[MarkdownV2 | None, str]:
        rich = to_md(markdown)
        return (MarkdownV2(rich) if len(rich) <= MAX_MSG else None), markdown

    def render_pages(self, pages: ui.Pages) -> list[tuple[MarkdownV2 | None, str]]:
        return [
            ((MarkdownV2(md) if len(md) <= MAX_MSG else None), plain)
            for md, plain in pages_md(pages)
        ]

    @staticmethod
    def measure(markdown: str) -> int:
        """The MarkdownV2 size of standard Markdown (what `send_rich` splits against)."""
        return len(to_md(markdown))

    @staticmethod
    def page_size(head: str, body: str, first: bool) -> int:
        """The MarkdownV2 size of one page (what `ui.expandable_pages` packs against)."""
        return len(_page(head, body, first)[0])

    def classify(self, error: Exception):
        if isinstance(error, TelegramRetryAfter):
            return Flood(seconds=error.retry_after)
        if isinstance(error, TelegramBadRequest):
            if "message is not modified" in str(error):
                return NotModified()
            if "message to delete not found" in str(error):
                return NotFound()
            return Refused()
        if isinstance(error, (TelegramNetworkError, TelegramServerError)):
            return Transient()
        if isinstance(error, TelegramAPIError):
            return Refused()
        return None

    # ── Calls ──────────────────────────────────────────────────────

    def _text_kw(self, text, buttons) -> tuple[str, dict]:
        kw = {}
        if isinstance(text, MarkdownV2):
            kw["parse_mode"] = "MarkdownV2"
        markup = _markup(buttons)
        if markup is not None:
            kw["reply_markup"] = markup
        return str(text), kw

    async def send(self, window, text, buttons, reply_to, silent=False, thread=None):
        body, kw = self._text_kw(text, buttons)
        kw.update(self._where(window))
        if reply_to is not None:
            kw["reply_parameters"] = ReplyParameters(message_id=reply_to)
        if silent:
            kw["disable_notification"] = True
        message = await self.bot.send_message(self._chat, body, **kw)
        return message.message_id

    async def edit(self, window, message_id, text, buttons):
        if text is None:
            kw = {}
            markup = _markup(buttons)
            if markup is not None:
                kw["reply_markup"] = markup
            await self.bot.edit_message_reply_markup(
                chat_id=self._chat, message_id=message_id, **kw
            )
            return True
        body, kw = self._text_kw(text, buttons)
        await self.bot.edit_message_text(body, chat_id=self._chat, message_id=message_id, **kw)
        return True

    async def delete(self, window, message_id):
        await self.bot.delete_message(self._chat, message_id)
        return True

    async def send_file(self, window, path: Path, caption):
        kw = self._where(window)
        if caption:
            kw["caption"] = caption
        data = BufferedInputFile(path.read_bytes(), path.name)
        message = await self.bot.send_document(self._chat, data, **kw)
        return message.message_id

    async def send_photo(self, window, data: bytes, caption, buttons=None, name="image.png"):
        kw = self._where(window)
        if caption:
            kw["caption"] = caption
        markup = _markup(buttons)
        if markup is not None:
            kw["reply_markup"] = markup
        message = await self.bot.send_photo(self._chat, BufferedInputFile(data, name), **kw)
        return message.message_id

    async def edit_photo(self, window, message_id, data: bytes, buttons=None, name="image.png"):
        media = InputMediaPhoto(media=BufferedInputFile(data, name))
        await self.bot.edit_message_media(
            media=media, chat_id=self._chat, message_id=message_id, reply_markup=_markup(buttons)
        )
        return True

    async def download(self, attachment: Attachment, dest: Path) -> Path:
        info = await self.bot.get_file(attachment.handle)
        buf = await self.bot.download_file(info.file_path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(buf.read())
        return dest

    async def create_window(self, title: str) -> str:
        topic = await self.bot.create_forum_topic(chat_id=self._chat, name=title)
        return str(topic.message_thread_id)

    async def rename_window(self, window, title: str):
        await self.bot.edit_forum_topic(
            chat_id=self._chat, message_thread_id=self._thread(window), name=title
        )
        return True

    async def close_window(self, window):
        await self.bot.delete_forum_topic(
            chat_id=self._chat, message_thread_id=self._thread(window)
        )
        return True

    async def window_alive(self, window, title: str | None) -> bool:
        """Probe a topic invisibly by re-setting its own current title (a no-op edit, so
        no "typing…" flicker every sweep); only an explicit 'thread not found' means gone.
        Untitled → the visible typing probe."""
        try:
            if title:
                await self.bot.edit_forum_topic(
                    chat_id=self._chat, message_thread_id=self._thread(window), name=title
                )
            else:
                await self.bot.send_chat_action(
                    chat_id=self._chat, action="typing", message_thread_id=self._thread(window)
                )
            return True
        except TelegramAPIError as e:
            return "not found" not in str(e).lower()

    async def answer_tap(self, tap: Tap):
        await self.bot.answer_callback_query(tap.tap_id)
        return True

    async def identity(self) -> str:
        me = await self.bot.get_me()
        self.bot_id = getattr(me, "id", None)
        self.config.bot_username = me.username
        self.config.save()
        return me.username

    async def bind_owner(self, user_id) -> None:
        """Bind the owner and the forum group they typed `/init` in."""
        self.config.owner_id = user_id
        self.config.chat_id = self._init_chats.get(user_id)
        self.config.save()

    # ── Updates ────────────────────────────────────────────────────

    def to_inbound(self, message) -> Inbound | None:
        """A Telegram message as an `Inbound`; None for another group's message."""
        chat = getattr(message, "chat", None)
        chat_id = getattr(chat, "id", None)
        user = getattr(message, "from_user", None)
        user_id = user.id if user is not None else None
        if self._chat is None:
            if user_id is not None:
                self._init_chats[user_id] = chat_id
        elif chat_id != self._chat:
            return None
        text = getattr(message, "text", None) or getattr(message, "caption", None) or ""
        mention = f"@{self.config.bot_username}"
        mentioned = bool(self.config.bot_username) and mention in text
        if mentioned:
            text = text.replace(mention, "")
        text = _COMMAND_TARGET.sub(r"\1", text)
        inbound = Inbound(
            window=self.window_of(getattr(message, "message_thread_id", None)),
            message_id=getattr(message, "message_id", None),
            user_id=user_id,
            text=text,
            mentioned=mentioned,
            forward_source=_forward_source(message),
            album=getattr(message, "media_group_id", None),
            attachments=_attachments(message),
            mentioned_user_ids=_mentioned_user_ids(message, self.bot_id),
        )
        reply = getattr(message, "reply_to_message", None)
        if reply is not None and not getattr(reply, "forum_topic_created", None):
            author = getattr(reply, "from_user", None)
            inbound.replied_text = (
                getattr(reply, "text", None) or getattr(reply, "caption", None) or ""
            ).strip()
            inbound.replied_author = getattr(author, "full_name", None) if author else None
            inbound.replied_user_id = getattr(author, "id", None)
            inbound.replied_attachments = _attachments(reply)
            quote = getattr(message, "quote", None)
            inbound.quote = getattr(quote, "text", None) if quote is not None else None
        return inbound

    def to_tap(self, callback) -> Tap:
        message = getattr(callback, "message", None)
        user = getattr(callback, "from_user", None)
        return Tap(
            window=self.window_of(getattr(message, "message_thread_id", None))
            if message is not None
            else GENERAL,
            message_id=getattr(message, "message_id", None) if message is not None else None,
            data=getattr(callback, "data", None) or "",
            user_id=user.id if user is not None else None,
            tap_id=str(getattr(callback, "id", "") or ""),
        )

    async def run(self, on_message, on_tap) -> None:
        """Long-poll Telegram until cancelled, handing each update over as an `Inbound`
        or a `Tap`."""
        dispatcher = Dispatcher()
        router = Router(name="tgforge")

        @router.message()
        async def _on_message(message: Message):
            inbound = self.to_inbound(message)
            if inbound is not None:
                await on_message(inbound)

        @router.callback_query()
        async def _on_callback(callback: CallbackQuery):
            await on_tap(self.to_tap(callback))

        dispatcher.include_router(router)
        # the session stays open for the drain after polling; `close` ends it
        await dispatcher.start_polling(self.bot, handle_signals=False, close_bot_session=False)

    async def close(self) -> None:
        """Close the bot's HTTP session (after the drain)."""
        await self.bot.session.close()
