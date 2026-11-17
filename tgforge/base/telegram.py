"""The Telegram adapter: every aiogram call, update conversion and Telegram rule.

A window is a forum topic: its key is the thread id as a decimal string, General is
"0". Every message goes out as a Rich Message (`sendRichMessage`, edits through
`editMessageText`'s `rich_message`): Markdown renders to Rich HTML through `rich_html`
(telegramify-markdown's rich backend, `$…$` math and `<sub>`/`<sup>` kept); when
Telegram refuses it, the message goes again as Rich with its diagrams as code (when it
has drawn ones), then as plain text in an escaped Rich Message, then as plain text.
`ui.Pages` render as one message per page (the summary above a collapsed block, then
`↳ i/n` blocks); a `ui.Keyboard` renders as an inline keyboard. The kernel's
`Transport` paces, retries and orders the calls made here, walking `bodies` in order.
"""

from __future__ import annotations

import functools
import html
import logging
import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime
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
    InputRichMessage,
    InputRichMessageMedia,
    Message,
    ReplyParameters,
)
from telegramify_markdown.converter import _escape_at, _parse
from telegramify_markdown.rich import (
    RICH_BLOCK_LIMIT,
    RICH_BYTE_LIMIT,
    RICH_OPTIONS,
    _RichHtmlWalker,
)

from tgforge.base import ui
from tgforge.base.config import BotConfig
from tgforge.base.kernel import (
    NOTICE_FALLBACK,
    REQUEST_TIMEOUT,
    Attachment,
    Flood,
    Inbound,
    NotFound,
    Notice,
    NotModified,
    Refused,
    Tap,
    Transient,
)
from tgforge.base.mermaid import DiagramError, MermaidRenderer, diagram_files
from tgforge.base.ui import MAX_MSG

LOGGER = logging.getLogger("tgforge")


# ── Pages ──────────────────────────────────────────────────────────


def _page(head: str, body: str, first: bool) -> str:
    """The plain text of one page: page 1 is `head` with `body` under it; a later page is
    `head`, a blank line, then `body`."""
    if not first:
        return head + (f"\n\n{body}" if body else "")
    return f"{head}\n{body}" if body else head


def pages_plain(pages: ui.Pages) -> list[str]:
    """The plain text per page (the layout of `pages_rich`), the note a blank line below
    page 1."""
    out = [_page(ui.page_head(pages, i), body, i == 0) for i, body in enumerate(pages.pages)]
    if pages.note and out:
        out[0] += f"\n\n{pages.note}"
    return out


# ── Rich Messages ──────────────────────────────────────────────────

# closers of the inline elements raw HTML opens (sub/superscripts, a spoiler)
_RAW_CLOSERS = ("</sub>", "</sup>", "</tg-spoiler>")
# a Rich HTML tag that opens a block (the 500-block limit counts nested ones too)
_BLOCK_TAG = re.compile(
    r"<(?:p|h[1-6]|pre|hr|ul|ol|li|blockquote|table|tr|details|tg-math-block|footer|aside|img)\b"
)
# a Rich HTML photo, or a Mermaid code block that is sent as one (each counts against
# the 50-media limit)
_MEDIA = re.compile(r"<img\b|<code class=\"language-mermaid\">")
RICH_MEDIA_LIMIT = 50  # photos and other media in one Rich Message
# a Mermaid code block in Rich HTML, its source escaped
_DIAGRAM = re.compile(r'<pre><code class="language-mermaid">(.*?)</code></pre>', re.DOTALL)
# the name run never gives back; a tag's attributes stop at the next `<`
_TAG = re.compile(r"<(/?)([a-zA-Z][\w-]*+)[^<>]*?(/?)>")
_VOID_TAGS = ("br", "hr", "input", "img")
_ROW_TOKEN = re.compile(r"</?tr>|<t[hd][\s>]")  # a row's start or end, or a cell's start
RICH_DEPTH_LIMIT = 16  # levels of nested formatting and blocks
RICH_COLUMN_LIMIT = 20  # columns in a table
FLAT_ITEM_INDENT = "\u00a0\u00a0"  # per flattened list level, before the item marker


class _DroppedCloser(str):
    """The closer of an inline element written without its tags (past the depth
    limit): empty, so closing it writes nothing; `tag` is the closer it stands for."""

    tag: str = ""


def _closer_tag(closer: str) -> str:
    return closer.tag if isinstance(closer, _DroppedCloser) else closer


class RichMessageWalker(_RichHtmlWalker):
    """telegramify-markdown's Rich HTML walker, changed for Telegram's Rich Messages:
    inline `<sub>`/`<sup>` stay sub/superscripts and `<tg-spoiler>` a spoiler (any
    other raw HTML stays literal text; a stray closing tag stays literal, an unclosed
    one is closed before whatever ends around it); a list or quote nests only while the
    deepest block inside it still fits RICH_DEPTH_LIMIT levels (`_container_needs`),
    else it is flattened (each item a paragraph with an indented marker); an inline
    element (bold, italic, strike, sub/sup, a link, a spoiler, inline code or math, a
    footnote reference), or display math met inside inline formatting, that would nest
    past the limit is written as its text alone, so
    one walk always fits the limit and keeps every word; a table wider than
    RICH_COLUMN_LIMIT goes out as several tables of those columns; text after display
    math in the same paragraph opens a new paragraph."""

    def __init__(self, needs: list[int]) -> None:
        super().__init__()
        self._needs = iter(needs)  # per list or quote, in order: its deepest block's levels
        self._nested_depth = 0  # open ul / ol, li and blockquote elements
        self._flat_lists: list[int | None] = []  # flattened lists: next number, None bullets
        self._quotes: list[bool] = []  # per open quote: whether its element was written
        self._headings = 0  # open heading elements
        self._footnotes = 0  # open footnote definitions

    def _depth_outside_paragraph(self) -> int:
        """How deep the elements open at the write position nest, a paragraph left out
        (a block closes it): lists, items and quotes, a heading or a footnote, the
        table, row and cell around a cell, and the inline elements."""
        return (
            self._nested_depth
            + self._headings
            + self._footnotes
            + (3 if self._in_table_cell else 0)
            + sum(1 for closer in self._inline_closers if closer)
        )

    def _element_depth(self) -> int:
        """`_depth_outside_paragraph` plus the paragraph (open, or opened next)."""
        paragraph = self._paragraph_open or (self._pending_paragraph and not self._in_table_cell)
        return self._depth_outside_paragraph() + (1 if paragraph else 0)

    def _open_inline(self, open_tag: str, close_tag: str) -> None:
        if self._element_depth() < RICH_DEPTH_LIMIT:
            super()._open_inline(open_tag, close_tag)
            return
        dropped = _DroppedCloser()
        dropped.tag = close_tag
        self._inline_closers.append(dropped)  # its text is written, its tags are not

    def _handle_event(self, event) -> None:
        payload = event[0] if isinstance(event, tuple) and len(event) == 2 else event
        if isinstance(payload, dict):
            if "TaskListMarker" in payload and self._flat_lists:
                self._write_inline("☑ " if payload["TaskListMarker"] else "☐ ")
                return
            if "DisplayMath" in payload and self._depth_outside_paragraph() >= RICH_DEPTH_LIMIT:
                math = payload["DisplayMath"]
                self._write_inline(self._escape_text("$$" + math + "$$"))
                return  # its text, without the tag
            if "DisplayMath" in payload:
                in_paragraph = self._paragraph_open
                super()._handle_event(event)
                self._pending_paragraph = in_paragraph
                return
            at_limit = self._element_depth() >= RICH_DEPTH_LIMIT
            if at_limit and "InlineMath" in payload:  # its text, without the tag
                self._write_inline(self._escape_text(f"${payload['InlineMath']}$"))
                return
            if at_limit and "FootnoteReference" in payload:
                self._write_inline(self._escape_text(f"[{payload['FootnoteReference']}]"))
                return
        super()._handle_event(event)

    def _on_inline_code(self, code: str) -> None:
        if self._image is None and self._element_depth() >= RICH_DEPTH_LIMIT:
            self._write_inline(self._escape_text(code))  # its text, without the tag
            return
        super()._on_inline_code(code)

    def _on_inline_html(self, value: str) -> None:
        tag = value.strip().lower()
        if tag in ("<sub>", "<sup>", "<tg-spoiler>"):
            self._open_inline(tag, "</" + tag[1:])
        elif tag in _RAW_CLOSERS and [_closer_tag(c) for c in self._inline_closers[-1:]] == [tag]:
            self._close_inline()
        else:
            super()._on_inline_html(value)

    def _close_open_raw(self) -> None:
        """Close the raw-HTML inline elements still open (never closed in the source)."""
        while self._inline_closers and _closer_tag(self._inline_closers[-1]) in _RAW_CLOSERS:
            self._close_inline()

    def _on_start(self, tag) -> None:
        if tag == "Item" and self._flat_lists:
            self._close_paragraph()
            number = self._flat_lists[-1]
            if number is not None:
                self._flat_lists[-1] = number + 1
            marker = "◦" if number is None else f"{number}."
            self._pending_paragraph = True
            self._write_inline(f"{FLAT_ITEM_INDENT * (len(self._flat_lists) - 1)}{marker} ")
            return
        if isinstance(tag, dict) and ("Heading" in tag or "FootnoteDefinition" in tag):
            super()._on_start(tag)
            if "Heading" in tag:
                self._headings += 1
            else:
                self._footnotes += 1
            return
        if isinstance(tag, dict) and "BlockQuote" in tag:
            need = next(self._needs)
            nested = self._nested_depth + self._footnotes + 1 + need <= RICH_DEPTH_LIMIT
            self._quotes.append(nested)
            if not nested:
                self._enter_block()
                self._close_paragraph()
                return
        super()._on_start(tag)
        if tag == "Item" or (isinstance(tag, dict) and "BlockQuote" in tag):
            self._nested_depth += 1

    def _on_end(self, tag) -> None:
        self._close_open_raw()
        if tag == "FootnoteDefinition" or (isinstance(tag, dict) and "Heading" in tag):
            super()._on_end(tag)
            if tag == "FootnoteDefinition":
                self._footnotes -= 1
            else:
                self._headings -= 1
            return
        if tag == "Item" and self._flat_lists:
            self._close_paragraph()
            return
        if isinstance(tag, dict) and "BlockQuote" in tag and not self._quotes.pop():
            self._close_paragraph()
            self._leave_block()
            return
        super()._on_end(tag)
        if tag == "Item" or (isinstance(tag, dict) and "BlockQuote" in tag):
            self._nested_depth -= 1

    def _on_start_list(self, start_number: int | None) -> None:
        need = next(self._needs)
        depth = self._nested_depth + self._footnotes  # the open containers around it
        if self._flat_lists or depth + 2 + need > RICH_DEPTH_LIMIT:
            self._close_paragraph()
            self._flat_lists.append(start_number)
            return
        super()._on_start_list(start_number)
        self._nested_depth += 1

    def _on_end_list(self) -> None:
        if self._flat_lists:
            self._close_paragraph()
            self._flat_lists.pop()
            return
        super()._on_end_list()
        self._nested_depth -= 1

    def _on_end_table(self) -> None:
        rows, alignments = self._table_rows, self._table_alignments
        width = max((len(row) for row in rows), default=0)
        if width <= RICH_COLUMN_LIMIT:
            super()._on_end_table()
            return
        for first in range(0, width, RICH_COLUMN_LIMIT):
            last = first + RICH_COLUMN_LIMIT
            self._table_rows = [row[first:last] or [("", False)] for row in rows]
            self._table_alignments = alignments[first:last]
            super()._on_end_table()

    def _close_paragraph(self) -> None:
        self._close_open_raw()
        super()._close_paragraph()


def _price_openers(text: str, events: tuple) -> list[int]:
    """UTF-8 byte offsets of each `$` opening an inline math span that is a price: a
    digit right after it, a letter, `\\` or `{` right after its closing `$` (that `$`
    opens the symbol written after the price), and before that closing `$` a character
    a formula does not end on here (not a letter, digit, `}` or `)`)."""
    source = text.encode()
    offsets = []
    for payload, source_range in events:
        if isinstance(payload, dict) and "InlineMath" in payload:
            start, end = source_range["start"], source_range["end"]
            after = source[end : end + 4].decode("utf-8", "ignore")[:1]
            before = source[max(start, end - 5) : end - 1].decode("utf-8", "ignore")[-1:]
            symbol_follows = after.isalpha() or after in ("\\", "{")
            formula_ends = before.isalnum() or before in ("}", ")")
            if source[start + 1 : start + 2].isdigit() and symbol_follows and not formula_ends:
                offsets.append(start)
    return offsets


@functools.lru_cache(maxsize=8)  # a piece's measure and its send render the same text
def rich_html(text: str) -> str:
    """GFM → Telegram Rich HTML: `$…$` / `$$…$$` as `<tg-math>` / `<tg-math-block>`
    (a price's `$` before a symbol stays text), `<sub>`/`<sup>` as sub/superscripts,
    other raw HTML as literal text; within RICH_DEPTH_LIMIT levels in one walk (see
    `RichMessageWalker`)."""
    parsed, events = _parse(text, RICH_OPTIONS, latex_escape=False)
    while offsets := _price_openers(parsed, events):
        parsed, events = _parse(_escape_at(parsed, offsets), RICH_OPTIONS, latex_escape=False)
    return RichMessageWalker(_container_needs(events)).walk(events)


# the levels a block written inside a list or quote takes: a table, row and cell; a
# code block's pre and code; any other block (an item written as a paragraph included)
_BLOCK_LEVELS = {"Table": 3, "CodeBlock": 2, "Paragraph": 1, "Heading": 1, "HtmlBlock": 1}
_BLOCK_LEVELS |= {"Item": 1, "FootnoteDefinition": 2}


def _event_name(tag) -> str:
    return tag if isinstance(tag, str) else next(iter(tag))


def _container_needs(events) -> list[int]:
    """Per list and blockquote, in the order they start: the most levels a block inside
    it takes (its inner lists and quotes left out: each nests only where its own
    blocks still fit, else is flattened)."""
    needs: list[int] = []
    open_containers: list[int] = []
    for event in events:
        payload = event[0] if isinstance(event, tuple) and len(event) == 2 else event
        if payload == "Rule" or (isinstance(payload, dict) and "DisplayMath" in payload):
            levels = 1
        elif isinstance(payload, dict) and "Start" in payload:
            name = _event_name(payload["Start"])
            if name in ("List", "BlockQuote"):
                open_containers.append(len(needs))
                needs.append(0)
                continue
            levels = _BLOCK_LEVELS.get(name, 0)
        elif isinstance(payload, dict) and "End" in payload:
            if _event_name(payload["End"]) in ("List", "BlockQuote"):
                open_containers.pop()
            continue
        else:
            continue
        for container in open_containers:
            needs[container] = max(needs[container], levels)
    return needs


@functools.lru_cache(maxsize=16)  # the piece a split chose is rendered for its send next
def _measure(markdown: str) -> int:
    """`TelegramAdapter.measure`; a size past MAX_MSG once a form is known to be over (the
    plain length, then the Rich size, depth and columns)."""
    if len(markdown) > MAX_MSG:
        return len(markdown)
    rich = rich_html(markdown)
    size = rich_size(rich)
    if size > MAX_MSG:
        return size
    if rich_depth(rich) > RICH_DEPTH_LIMIT or rich_columns(rich) > RICH_COLUMN_LIMIT:
        return MAX_MSG + 1
    return max(len(markdown), size)


def _html_lines(text: str) -> str:
    return "<br/>".join(html.escape(line, quote=False) for line in text.split("\n"))


def plain_html(text: str) -> str:
    """Plain text as Rich HTML showing the same characters and line breaks."""
    return f"<p>{_html_lines(text)}</p>"


def _rich_page(head: str, body: str, first: bool) -> str:
    """The Rich HTML of one page (the layout of `_page`)."""
    if not first:
        rest = f"<br/><br/>{_html_lines(body)}" if body else ""
        return f"<blockquote expandable>{_html_lines(head)}{rest}</blockquote>"
    quote = f"<blockquote expandable>{_html_lines(body)}</blockquote>" if body else ""
    return f"<p>{_html_lines(head)}</p>{quote}"


def pages_rich(pages: ui.Pages) -> list[str]:
    """Rich HTML per page, the layout of `pages_plain`."""
    out = [_rich_page(ui.page_head(pages, i), body, i == 0) for i, body in enumerate(pages.pages)]
    if pages.note and out:
        out[0] += plain_html(pages.note)
    return out


def rich_blocks(rich: str) -> int:
    """The blocks a Rich HTML body counts against the 500-block limit (nested included)."""
    return len(_BLOCK_TAG.findall(rich))


def rich_depth(rich: str) -> int:
    """The deepest nesting of elements in Rich HTML (formatting and blocks alike)."""
    depth = deepest = 0
    for closing, name, self_closing in _TAG.findall(rich):
        if self_closing or name.lower() in _VOID_TAGS:
            continue
        depth += -1 if closing else 1
        deepest = max(deepest, depth)
    return deepest


def rich_columns(rich: str) -> int:
    """The most cells in one table row of Rich HTML (one pass over its row and cell tags)."""
    widest = cells = 0
    for token in _ROW_TOKEN.finditer(rich):
        tag = token.group()
        if tag == "<tr>":
            cells = 0
        elif tag == "</tr>":
            widest = max(widest, cells)
        else:
            cells += 1
    return widest


def rich_media(rich: str) -> int:
    """The media a Rich HTML body counts against the 50-media limit: its photos and its
    Mermaid code blocks (each sent as a photo)."""
    return len(_MEDIA.findall(rich))


def rich_fits(rich: str) -> bool:
    """Whether Rich HTML is within the Rich Message limits (bytes, blocks, media, nesting
    depth, table columns)."""
    return (
        len(rich.encode()) <= RICH_BYTE_LIMIT
        and rich_blocks(rich) <= RICH_BLOCK_LIMIT
        and rich_media(rich) <= RICH_MEDIA_LIMIT
        and rich_depth(rich) <= RICH_DEPTH_LIMIT
        and rich_columns(rich) <= RICH_COLUMN_LIMIT
    )


def rich_size(rich: str) -> int:
    """Rich HTML's size on the MAX_MSG scale: its share of the byte, the block or the
    media limit, whichever is largest, times MAX_MSG — what the neutral splitters pack
    against (a Mermaid block measured as code: its photo is one block and fewer bytes)."""
    share = max(
        len(rich.encode()) / RICH_BYTE_LIMIT,
        rich_blocks(rich) / RICH_BLOCK_LIMIT,
        rich_media(rich) / RICH_MEDIA_LIMIT,
    )
    return math.ceil(share * MAX_MSG)


GENERAL = "0"  # General (outside any forum topic) has no thread id
_COMMAND_TARGET = re.compile(r"^(\s*/[^\s@]*)@\S*")  # the `@botname` of `/cmd@botname`


class PlainText(str):
    """A body sent as plain text (no parse mode), the last one tried."""


@dataclass
class RichMessage:
    """A body sent as a Rich Message: its HTML; `escaped`, plain text written as Rich
    HTML (the form after the rendered one); `images`, the (media id, PNG) the HTML's
    `tg://photo?id=` links name, uploaded with it; `code_html`, the HTML with those
    diagrams as code (the Rich form tried when Telegram refuses the drawn one)."""

    html: str
    escaped: bool = False
    images: tuple[tuple[str, bytes], ...] = ()
    code_html: str | None = None


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


_RICH_TEXT_FIELDS = ("summary", "text", "expression", "alternative_text", "button")
_RICH_BLOCK_FIELDS = ("blocks", "items", "buttons")
_RICH_AFTER_FIELDS = ("caption", "credit")


def _rich_plain(node) -> str:
    """The plain text of a received Rich Message, from its dumped blocks: each block on
    its own line, a list item after its label, a table row's cells joined by ` | `, a
    caption or credit after what it labels."""
    if isinstance(node, str):
        return node
    if isinstance(node, list):  # a rich text's pieces
        return "".join(_rich_plain(piece) for piece in node)
    if not isinstance(node, dict):
        return ""
    parts = [_rich_plain(node[field]) for field in _RICH_TEXT_FIELDS if field in node]
    for field in _RICH_BLOCK_FIELDS:
        parts += [_rich_plain(block) for block in node.get(field, [])]
    for row in node.get("cells", []):
        parts.append(" | ".join(_rich_plain(cell) for cell in row))
    parts += [_rich_plain(node[field]) for field in _RICH_AFTER_FIELDS if field in node]
    text = "\n".join(part for part in parts if part)
    label = node.get("label")  # a list item's number or bullet
    return f"{label} {text}" if label else text


def _message_text(message) -> str:
    """A message's text: its text, its caption, or its Rich Message's plain text."""
    text = getattr(message, "text", None) or getattr(message, "caption", None)
    if text:
        return text
    rich = getattr(message, "rich_message", None)
    return _rich_plain(rich.model_dump(mode="json", exclude_none=True)) if rich else ""


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
    renders_tables = True  # a Markdown table renders as a Rich table
    max_message = MAX_MSG  # the most characters one message's Markdown may span
    photo_max = 10 * 1024 * 1024
    upload_max = 50 * 1024 * 1024
    download_max = 20 * 1024 * 1024  # the bot API's getFile cap

    def __init__(self, bot, config: BotConfig):
        self.bot = bot
        self.config = config
        self._init_chats: dict[int, int] = {}  # before /init: user id → the chat they wrote in
        self.bot_id: int | None = None  # learned by `identity()`
        # draws Mermaid blocks with the files fetched into the bot home; opens on use
        self.diagrams = MermaidRenderer(diagram_files(config.home_path) if config else None)

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

    def render(self, markdown: str) -> tuple[RichMessage | None, str]:
        """Markdown's Rich body, or None when it is over one message in one of its forms
        (`measure`): the plain body goes instead, never a cut."""
        if _measure(markdown) > MAX_MSG:  # the split's measure of this piece, reused
            return None, markdown
        return RichMessage(rich_html(markdown)), markdown

    def render_pages(self, pages: ui.Pages) -> list[tuple[RichMessage | None, str]]:
        return [
            (RichMessage(rich) if rich_fits(rich) else None, plain)
            for rich, plain in zip(pages_rich(pages), pages_plain(pages), strict=True)
        ]

    @staticmethod
    def measure(markdown: str) -> int:
        """The size of standard Markdown in its largest form, on the MAX_MSG scale (what
        `send_rich` splits against): its plain length and its Rich HTML's `rich_size`,
        past MAX_MSG when the Rich HTML breaks the depth or column limit — so a piece
        within MAX_MSG goes out as one message in every form."""
        return _measure(markdown)

    @staticmethod
    def page_size(head: str, body: str, first: bool) -> int:
        """The plain length of one page (what `ui.expandable_pages` packs against); its
        Rich page (at most 3 blocks, at most 6 bytes per plain character: `&amp;`, `<br/>`
        for a newline, 4 for a character outside the BMP) fits the Rich Message limits
        whenever this does."""
        return len(_page(head, body, first))

    @staticmethod
    def bodies(body):
        """The forms to try in order for one message, each one message (built when
        reached): a rich body (with drawn diagrams, then with them as code; `render`
        gives one only for Markdown that fits MAX_MSG in every form); plain text (within
        MAX_MSG, the caller's bound) as escaped Rich HTML, then as plain text."""
        if isinstance(body, RichMessage):
            yield body
            if body.code_html is not None:  # drawn diagrams refused: the diagrams as code
                yield RichMessage(body.code_html)
            return
        if isinstance(body, PlainText):
            yield body
            return
        yield RichMessage(plain_html(body), escaped=True)
        yield PlainText(body)

    def prepare(self) -> None:
        """Fetch whichever diagram file is missing, off the event loop (at startup)."""
        self.diagrams.prepare()

    async def draw_diagrams(self, window, body):
        """`body` with each Mermaid code block of its Rich HTML drawn as a photo in its
        place (uploaded with the message); a diagram that does not draw stays code and
        raises a fallback notice naming why. The later forms keep the code."""
        if not isinstance(body, RichMessage):
            return body
        if '<code class="language-mermaid">' not in body.html:
            return body
        parts: list[str] = []
        images: list[tuple[str, bytes]] = []
        last = 0
        for match in _DIAGRAM.finditer(body.html):
            source = html.unescape(match.group(1))
            try:
                png = await self.diagrams.draw(source)
            except DiagramError as error:
                self.on_notice(
                    Notice(
                        NOTICE_FALLBACK, window, "diagram", "image → code", str(error), len(source)
                    )
                )
                continue
            media_id = f"diagram{len(images) + 1}"
            images.append((media_id, png))
            parts += [body.html[last : match.start()], f'<img src="tg://photo?id={media_id}"/>']
            last = match.end()
        if not images:
            return body
        parts.append(body.html[last:])
        drawn = "".join(parts)
        return RichMessage(drawn, images=tuple(images), code_html=body.html)

    @staticmethod
    def form_name(body) -> str:
        """A body's form, for a notice: Rich with images (drawn diagrams), Rich, escaped
        Rich (plain text) or plain."""
        if isinstance(body, RichMessage):
            if body.images:
                return "Rich with images"
            return "escaped Rich" if body.escaped else "Rich"
        return "plain"

    @staticmethod
    def payload_size(body) -> int:
        """A body's size in characters, for a notice (a Rich Message's HTML)."""
        if isinstance(body, RichMessage):
            return len(body.html)
        return len(str(body))

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

    async def _rich_call(self, rich: RichMessage, call):
        """Make a Rich Message call; a refusal is a rendering defect: logged with its
        error and the payload size, then raised for the transport's next body."""
        try:
            media = [
                InputRichMessageMedia(
                    id=media_id,
                    media=InputMediaPhoto(media=BufferedInputFile(png, f"{media_id}.png")),
                )
                for media_id, png in rich.images
            ]
            return await call(InputRichMessage(html=rich.html, media=media or None))
        except TelegramAPIError as error:
            if isinstance(self.classify(error), Refused):
                LOGGER.warning(
                    "rich message refused (%d bytes, %d blocks): %s",
                    len(rich.html.encode()),
                    rich_blocks(rich.html),
                    getattr(error, "message", type(error).__name__),
                )
            raise

    async def send(self, window, text, buttons, reply_to, silent=False, thread=None):
        kw = self._where(window)
        markup = _markup(buttons)
        if markup is not None:
            kw["reply_markup"] = markup
        if reply_to is not None:
            kw["reply_parameters"] = ReplyParameters(message_id=reply_to)
        if silent:
            kw["disable_notification"] = True
        if isinstance(text, RichMessage):
            message = await self._rich_call(
                text, lambda rich: self.bot.send_rich_message(self._chat, rich, **kw)
            )
        else:
            message = await self.bot.send_message(self._chat, str(text), **kw)
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
        if isinstance(text, RichMessage):
            markup = _markup(buttons)
            kw = {"reply_markup": markup} if markup is not None else {}
            await self._rich_call(
                text,
                lambda rich: self.bot.edit_message_text(
                    chat_id=self._chat, message_id=message_id, rich_message=rich, **kw
                ),
            )
            return True
        markup = _markup(buttons)
        kw = {"reply_markup": markup} if markup is not None else {}
        await self.bot.edit_message_text(str(text), chat_id=self._chat, message_id=message_id, **kw)
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
        text = _message_text(message)
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
            inbound.replied_text = _message_text(reply).strip()
            inbound.replied_author = getattr(author, "full_name", None) if author else None
            inbound.replied_user_id = getattr(author, "id", None)
            inbound.replied_attachments = _attachments(reply)
            quote = getattr(message, "quote", None)
            inbound.quote = getattr(quote, "text", None) if quote is not None else None
        sent = getattr(message, "date", None)
        if sent is not None:  # a late delivery reads off this line
            age = (datetime.now(UTC) - sent).total_seconds()
            LOGGER.info(
                "inbound msg %s in %s: %d attachment(s), received %.1fs after sent",
                inbound.message_id,
                inbound.window,
                len(inbound.attachments),
                age,
            )
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
        """Close the diagram browser and the bot's HTTP session (after the drain)."""
        await self.diagrams.close()
        await self.bot.session.close()
