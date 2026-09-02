"""Base UI: downstream-agnostic Telegram rendering + inline-keyboard components.

Text/markdown helpers (chunking, MarkdownV2, expandable blocks, shell views,
durations) and keyboard builders. Apps compose these; no consumer knowledge.
A `cta` button carries a cross-app action id so a button shown by one app can
trigger a function another app registered — the bot core routes it.
"""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

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


def to_md(text: str) -> str:
    """GFM → Telegram MarkdownV2. Returns the text unchanged on any converter
    error; the send path then retries the message plain (no parse_mode)."""
    try:
        import telegramify_markdown

        return telegramify_markdown.markdownify(text)
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


def expandable(header: str, body: str) -> str:
    """A collapsed-by-default MarkdownV2 expandable blockquote: `header` is the
    always-shown first line, `body` the hidden-until-tapped remainder."""
    lines = [header] + (["", *body.split("\n")] if body else [])
    rows = [("**>" if i == 0 else ">") + mdv2_escape(ln) for i, ln in enumerate(lines)]
    return "\n".join(rows) + "||"


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
