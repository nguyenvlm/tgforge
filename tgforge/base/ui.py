"""Base UI: platform-neutral text helpers and button components.

Plugins compose standard Markdown, `Pages` and neutral `Button` / `Keyboard` values;
a window's adapter renders them for its platform and supplies the size measures the
splitters here pack against. Also here: chunking, durations, shell views. A `cta`
button carries a cross-app action id so a button shown by one app can trigger a
function another app registered — the bot core routes it.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

MAX_MSG = 4096
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


# ── Text ───────────────────────────────────────────────────────────


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


def md_chunks(text: str, measure: Callable[[str], int], floor: int) -> list[str]:
    """Split Markdown so each piece's rendered size (`measure`, the adapter's) also fits
    MAX_MSG — a raw-length-only split can render past the limit and fall back to plain.
    Shrinks newline-first down to `floor`, a raw size whose render always fits."""
    if not text:
        return ["(empty)"]
    out = []
    while text:
        if len(text) <= MAX_MSG and measure(text) <= MAX_MSG:
            out.append(text)
            break
        cut = text.rfind("\n", 1, min(len(text), MAX_MSG))
        if cut == -1:
            cut = min(len(text), MAX_MSG)
        while cut > floor and measure(text[:cut]) > MAX_MSG:
            newline = text.rfind("\n", 1, cut)
            cut = newline if newline > floor else floor
        out.append(text[:cut])
        text = text[cut:].lstrip("\n")
    return out


@dataclass
class Pages:
    """A summary line plus page bodies, each body packed to fit one message under its
    head. The window's adapter lays them out (one message per page, or the summary
    with the pages in its thread)."""

    summary: str
    pages: list[str]
    note: str = ""  # a line shown after the first page's body


def page_head(pages: Pages, index: int) -> str:
    """The head of page `index`: the summary on page 1, `↳ i/n` after it."""
    return pages.summary if index == 0 else f"↳ {index + 1}/{len(pages.pages)}"


# the size of one page as the adapter renders it: (head, body, first page?) -> length
PageSize = Callable[[str, str, bool], int]


def expandable_pages(
    header: str, entries: list[str], page_size: PageSize, limit: int = MAX_MSG
) -> Pages:
    """Fold `entries` into as few pages as fit, nothing cut: page 1 sits under `header`,
    the rest are headed `↳ i/n`; an entry too long for one message is split across
    pages. Every page's rendered size (`page_size`, the adapter's) stays within `limit`."""
    count = 1
    while True:  # pack against the longest `↳ i/n` the page count needs, until it holds
        cont = f"↳ {count}/{count}"
        pages = _pack_pages(header, cont, entries, limit, page_size)
        if len(pages) <= count:
            break
        count = len(pages)
    return Pages(header, ["\n\n".join(page) for page in pages])


def _pack_pages(
    header: str, cont: str, entries: list[str], limit: int, page_size: PageSize
) -> list[list[str]]:
    parts: list[str] = []
    for entry in entries:
        parts.extend(_split_to_fit(entry, header, cont, limit, page_size))
    pages: list[list[str]] = [[]]
    for part in parts:
        first = len(pages) == 1
        body = "\n\n".join([*pages[-1], part])
        if pages[-1] and page_size(header if first else cont, body, first) > limit:
            pages.append([])
        pages[-1].append(part)
    return pages


def _split_to_fit(entry: str, header: str, cont: str, limit: int, page_size: PageSize) -> list[str]:
    """Split one entry into pieces that each fit page 1 under `header` and a later page
    under `cont`, cutting at a newline (else a space) when one is near."""

    def fits(text: str) -> bool:
        return all(page_size(h, text, f) <= limit for h, f in ((header, True), (cont, False)))

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


def code_block(text: str) -> str:
    """`text` as a standard Markdown fenced code block, its fence longer than any
    backtick run inside it."""
    longest = run = 0
    for char in text:
        run = run + 1 if char == "`" else 0
        longest = max(longest, run)
    fence = "`" * max(3, longest + 1)
    return f"{fence}\n{text}\n{fence}"


def shell_md(cmd: str, buf: list[str], status: str) -> str:
    """Live view of a shell run as standard Markdown: one code block, the output's tail
    kept to fit a message."""
    view = shell_view(cmd, buf, status)
    return code_block(view)


# ── Button components ──────────────────────────────────────────────


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


@dataclass
class Button:
    """One tap button: its label and the data a tap delivers."""

    label: str
    data: str


@dataclass
class Keyboard:
    """Rows of buttons attached to a message."""

    rows: list[list[Button]]


def button(label: str, data: str) -> Button:
    return Button(label=blabel(label), data=data)


def keyboard(rows: list[list[Button]]) -> Keyboard:
    return Keyboard(rows=rows)


def stacked(buttons: list[Button]) -> Keyboard:
    """One button per row."""
    return keyboard([[b] for b in buttons])


TWO_COL_MAX = 16  # labels this short pair two-across on a phone row


def stacked_choices(labels: list[str], prefix: str, solo_last: bool = False) -> Keyboard:
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


def cta(label: str, action: str, arg: str = "") -> Button:
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


def act(label: str, name: str, arg: str = "") -> Button:
    """A window-routed button. Its callback data `act:<name>:<arg>` is routed by
    the core to the tapped thread's live window instance, then to its `@action`."""
    return button(label, f"{ACT_PREFIX}:{name}:{arg}")


SUGGEST_BTN_MAX = 40  # full-width suggested-reply buttons read wider than a menu row


def suggestion_buttons(labels: list[str], token: str) -> Keyboard:
    """Full-width suggested-reply buttons (one per row), head-preserving so the start
    of each reply is always legible. Tap data routes to the `sug` action."""
    return keyboard(
        [
            [
                Button(
                    label=blabel(label, SUGGEST_BTN_MAX, ellipsis="end"),
                    data=f"{ACT_PREFIX}:sug:{token}:{i}",
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
