"""Base UI: downstream-agnostic Telegram rendering + inline-keyboard components.

Text/markdown helpers (chunking, MarkdownV2, expandable blocks, shell views,
durations) and keyboard builders. Apps compose these; no consumer knowledge.
A `cta` button carries a cross-app action id so a button shown by one app can
trigger a function another app registered — the bot core routes it.
"""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from telegramify_markdown import entities_to_markdownv2
from telegramify_markdown.config import get_runtime_config
from telegramify_markdown.converter import STANDARD_OPTIONS, EventWalker, _parse

MAX_MSG = 4096
SAFE_RAW_CHUNK = 1800  # raw size whose MarkdownV2 conversion always fits MAX_MSG
SHELL_CODE_MAX_CHARS = 600  # ! output within this renders as a monospace code
SHELL_CODE_MAX_LINES = 15  # block; a longer/taller run falls back to collapse
CTA_PREFIX = "cta"
ACT_PREFIX = "act"

_DURATION_UNITS = (
    ("y", 31536000),
    ("mo", 2592000),
    ("d", 86400),
    ("h", 3600),
    ("m", 60),
    ("s", 1),
)


# ── Text / MarkdownV2 ──────────────────────────────────────────────


def chunks(text: str) -> list[str]:
    if not text:
        return ["(empty)"]
    out = []
    while text:
        if len(text) <= MAX_MSG:
            out.append(text)
            break
        cut = text.rfind("\n", 1, MAX_MSG)
        if cut == -1:
            cut = MAX_MSG
        out.append(text[:cut])
        text = text[cut:].lstrip("\n")
    return out


_SPOILER_TAGS = ("<tg-spoiler>", "</tg-spoiler>")


class LayoutKeepingWalker(EventWalker):
    """telegramify-markdown's event walker, changed to keep the source layout:
    raw HTML stays as literal text; a list item's later lines and blocks sit right
    under its bullet, first line indented, and an item with more than one block is
    set off from what follows it in the list by a blank line, so each bullet reads as one group.

    A hanging indent never exceeds the source line's own indent, so the output adds
    no whitespace the input lacks (`md_chunks` relies on bounded growth)."""

    def __init__(self, config, source_markdown: str) -> None:
        super().__init__(config, source_markdown)
        self._event_range: dict[str, int] | None = None
        self._in_html_block = False
        self._html_block_lines = 0
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
        self._write_newlines(2 if self._gap_pending else 1)
        self._gap_pending = False
        super()._on_start_item()
        marker = (
            f"{ordered}{self._config.markdown_symbol.ordered_list_suffix} "
            if ordered is not None
            else f"{self._config.markdown_symbol.unordered_list_item} "
        )
        self._item_stack.append([self._buf.py_offset, len(self._item_indent + marker), False])

    def _on_task_list_marker(self, checked: bool) -> None:
        super()._on_task_list_marker(checked)
        symbol = self._config.markdown_symbol
        mark = symbol.task_completed if checked else symbol.task_uncompleted
        self._item_stack[-1][:2] = [self._buf.py_offset, len(f"{self._item_indent}{mark} ")]

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
        if not self._list_stack or not self._item_stack or self._in_table_cell:
            return ""
        width = self._item_stack[-1][1]
        return " " * min(width, self._source_line_indent(source_start, forward))

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


def md_chunks(text: str) -> list[str]:
    """Split for the MarkdownV2 send path so each piece's `to_md()` also fits
    MAX_MSG — a raw-length-only split can escape past the limit and get truncated
    to invalid markup (whole message drops to plain). Shrinks newline-first down to
    SAFE_RAW_CHUNK, whose conversion always fits (escaping at most doubles)."""
    if not text:
        return ["(empty)"]
    out = []
    while text:
        if len(text) <= MAX_MSG and len(to_md(text)) <= MAX_MSG:
            out.append(text)
            break
        cut = text.rfind("\n", 1, min(len(text), MAX_MSG))
        if cut == -1:
            cut = min(len(text), MAX_MSG)
        while cut > SAFE_RAW_CHUNK and len(to_md(text[:cut])) > MAX_MSG:
            newline = text.rfind("\n", 1, cut)
            cut = newline if newline > SAFE_RAW_CHUNK else SAFE_RAW_CHUNK
        out.append(text[:cut])
        text = text[cut:].lstrip("\n")
    return out


def mdv2_escape(text: str) -> str:
    return "".join("\\" + c if c in "_*[]()~`>#+-=|{}.!\\" else c for c in text)


def _collapsed(lines: list[str]) -> str:
    rows = [("**>" if i == 0 else ">") + mdv2_escape(ln) for i, ln in enumerate(lines)]
    return "\n".join(rows) + "||"


def expandable(header: str, body: str) -> str:
    """A collapsed-by-default MarkdownV2 expandable blockquote: `header` is the
    always-shown first line, `body` the hidden-until-tapped remainder."""
    return _collapsed([header] + (["", *body.split("\n")] if body else []))


def _page(head: str, body: str, first: bool) -> tuple[str, str]:
    """(md, plain) of one page: page 1 is `head` as a plain line with `body` collapsed
    under it (no block when empty); a later page is one expandable headed `head`."""
    if not first:
        return expandable(head, body), head + (f"\n\n{body}" if body else "")
    if not body:
        return mdv2_escape(head), head
    quote = _collapsed(body.split("\n"))
    return f"{mdv2_escape(head)}\n{quote}", f"{head}\n{body}"


def expandable_pages(
    header: str, entries: list[str], limit: int = MAX_MSG
) -> list[tuple[str, str]]:
    """Fold `entries` into as few messages as fit, nothing cut: page 1 is `header` as a
    plain line above its collapsed entries, the rest are collapsed blocks headed `↳ i/n`;
    an entry too long for one message is split across messages. Returns (md, plain) per
    page, each within `limit`."""
    count = 1
    while True:  # pack against the longest `↳ i/n` the page count needs, until it holds
        cont = f"↳ {count}/{count}"
        pages = _pack_pages(header, cont, entries, limit)
        if len(pages) <= count:
            break
        count = len(pages)
    return [
        _page(header if i == 0 else f"↳ {i + 1}/{len(pages)}", "\n\n".join(page), i == 0)
        for i, page in enumerate(pages)
    ]


def _pack_pages(header: str, cont: str, entries: list[str], limit: int) -> list[list[str]]:
    parts: list[str] = []
    for entry in entries:
        parts.extend(_split_to_fit(entry, header, cont, limit))
    pages: list[list[str]] = [[]]
    for part in parts:
        first = len(pages) == 1
        body = "\n\n".join([*pages[-1], part])
        if pages[-1] and len(_page(header if first else cont, body, first)[0]) > limit:
            pages.append([])
        pages[-1].append(part)
    return pages


def _split_to_fit(entry: str, header: str, cont: str, limit: int) -> list[str]:
    """Split one entry into pieces that each fit page 1 under `header` and a later page
    under `cont`, cutting at a newline (else a space) when one is near."""

    def fits(text: str) -> bool:
        return all(len(_page(h, text, f)[0]) <= limit for h, f in ((header, True), (cont, False)))

    pieces = []
    while not fits(entry):
        cut = min(len(entry), limit)
        while cut > 1 and not fits(entry[:cut]):
            cut = cut * 9 // 10
        for sep in ("\n", " "):
            at = entry.rfind(sep, cut // 2, cut)
            if at > 0:
                cut = at
                break
        pieces.append(entry[:cut].rstrip())
        entry = entry[cut:].lstrip()
    pieces.append(entry)
    return pieces


def fmt_duration(seconds: int) -> str:
    """Largest-unit-first duration across y/mo/d/h/m/s (30d month, 365d year),
    zero units dropped."""
    seconds = int(seconds)
    parts = []
    for label, size in _DURATION_UNITS:
        if seconds >= size:
            val, seconds = divmod(seconds, size)
            parts.append(f"{val}{label}")
    return " ".join(parts) if parts else "0s"


def shell_view(cmd: str, buf: list[str], status: str) -> str:
    """Render a shell run: prompt line, tail-truncated merged output, and a
    status footer ('…' while streaming). Bounded to MAX_MSG for one edit."""
    body = "".join(buf).strip()
    head = f"$ {cmd}\n"
    footer = f"\n[{status}]" if status else ""
    cap = max(0, MAX_MSG - len(head) - len(footer) - 16)
    if len(body) > cap:
        body = "…(truncated)\n" + body[-cap:]
    return f"{head}{body}{footer}".strip()


def shell_md(cmd: str, buf: list[str], status: str) -> str:
    """Live view of a shell run. Short output renders as a monospace code block;
    a long or tall run falls back to a collapsed expandable."""
    body = "".join(buf).strip()
    footer = f"\n[{status}]" if status else ""
    fits = len(body) <= SHELL_CODE_MAX_CHARS and body.count("\n") <= SHELL_CODE_MAX_LINES
    if fits:
        content = f"$ {cmd}\n{body}{footer}" if body else f"$ {cmd}{footer}"
        esc = content.replace("\\", "\\\\").replace("`", "\\`")
        return f"```\n{esc}\n```"
    cap = max(0, MAX_MSG - len(cmd) - 64)
    if len(body) > cap:
        body = "…(truncated)\n" + body[-cap:]
    return expandable(f"$ {cmd} · {status or '…'}", body)


# ── Inline-keyboard components ─────────────────────────────────────


# A narrow phone shows ~1 line per full-width inline button; a long label is
# clipped mid-word. Keep every button's text to a leading glyph + 1–3 words.
BTN_MAX = 24


def blabel(text: str, limit: int = BTN_MAX, ellipsis: str = "middle") -> str:
    """Clamp button text to a phone-safe width. `ellipsis="middle"` keeps the head
    and tail (a path/id stays recognisable); `"end"` keeps the head only (a suggested
    reply or sentence, whose intent is at the start)."""
    text = text.replace("\n", " ").strip()
    if len(text) <= limit:
        return text
    if ellipsis == "end":
        return text[: limit - 1].rstrip() + "…"
    head = (limit - 1) // 2
    tail = limit - 1 - head
    return f"{text[:head]}…{text[-tail:]}"


def button(label: str, callback_data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=blabel(label), callback_data=callback_data)


def keyboard(rows: list[list[InlineKeyboardButton]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=rows)


def stacked(buttons: list[InlineKeyboardButton]) -> InlineKeyboardMarkup:
    """One button per row."""
    return keyboard([[b] for b in buttons])


TWO_COL_MAX = 16  # labels this short pair two-across on a phone row


def stacked_choices(
    labels: list[str], prefix: str, solo_last: bool = False
) -> InlineKeyboardMarkup:
    """A single-choice menu; each button's callback data is `<prefix>:<index>` in the
    given order. Two buttons per row when every (paired) label is short enough to sit
    two-across on a phone, else one per row. `solo_last` keeps the final label (e.g.
    Back) on its own full-width row."""
    buttons = [button(label, f"{prefix}:{i}") for i, label in enumerate(labels)]
    if not buttons:
        return keyboard([])
    tail = [buttons.pop()] if solo_last else []
    paired = labels[:-1] if solo_last else labels
    cols = 2 if paired and all(len(text) <= TWO_COL_MAX for text in paired) else 1
    rows = [buttons[i : i + cols] for i in range(0, len(buttons), cols)]
    rows += [[b] for b in tail]
    return keyboard(rows)


# ── Menu components ────────────────────────────────────────────────

MENU_FALLBACK_ICON = "▫️"  # a command that declared no icon still gets a button
DESKTOP_MENU_TITLE = "🧭 Menu"  # the header on the desktop / General (never a lone emoji)
BACK_LABEL = "⬅ Back"


def menu_glyph(icon: str, name: str) -> str:
    """A menu button's label: the command's icon (or the fallback) + its name."""
    return f"{icon or MENU_FALLBACK_ICON} {name.lstrip('/').title()}"


def triggered(label: str) -> str:
    """How a tapped menu marks a non-inline command that took over (no Back)."""
    return f"▸ {label}"


def cta(label: str, action: str, arg: str = "") -> InlineKeyboardButton:
    """A call-to-action button. Its callback data `cta:<action>:<arg>` is routed
    by the bot core to whichever app registered `action` — enabling cross-app
    calls (e.g. a shell result's 'hand to the agent' button)."""
    return button(label, f"{CTA_PREFIX}:{action}:{arg}")


def parse_cta(data: str) -> tuple[str, str] | None:
    """(action, arg) for a `cta:` callback, else None."""
    if not data.startswith(CTA_PREFIX + ":"):
        return None
    parts = data.split(":", 2)
    return parts[1], (parts[2] if len(parts) > 2 else "")


def act(label: str, name: str, arg: str = "") -> InlineKeyboardButton:
    """A window-routed button. Its callback data `act:<name>:<arg>` is routed by
    the core to the tapped thread's live window instance, then to its `@action`."""
    return button(label, f"{ACT_PREFIX}:{name}:{arg}")


SUGGEST_BTN_MAX = 40  # full-width suggested-reply buttons read wider than a menu row


def suggestion_buttons(labels: list[str], token: str) -> InlineKeyboardMarkup:
    """Full-width suggested-reply buttons (one per row), head-preserving so the start
    of each reply is always legible. Callback data routes to the `sug` action."""
    return keyboard(
        [
            [
                InlineKeyboardButton(
                    text=blabel(label, SUGGEST_BTN_MAX, ellipsis="end"),
                    callback_data=f"{ACT_PREFIX}:sug:{token}:{i}",
                )
            ]
            for i, label in enumerate(labels)
        ]
    )


def parse_act(data: str) -> tuple[str, str] | None:
    """(name, arg) for an `act:` callback, else None."""
    if not data.startswith(ACT_PREFIX + ":"):
        return None
    parts = data.split(":", 2)
    return parts[1], (parts[2] if len(parts) > 2 else "")
