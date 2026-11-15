"""Base UI: platform-neutral text helpers and button components.

Plugins compose standard Markdown, `Pages` and neutral `Button` / `Keyboard` values;
a window's adapter renders them for its platform and supplies the size measures the
splitters here pack against. Also here: chunking, durations, shell views. A `cta`
button carries a cross-app action id so a button shown by one app can trigger a
function another app registered — the bot core routes it.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

MAX_MSG = 4096
FULL_ENOUGH = MAX_MSG * 15 // 16  # a fitting piece at least this size ends the search
HALF_FULL = MAX_MSG // 2  # a preferred cut (whitespace, an unindented line) fills at least this
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


def md_chunks(text: str, measure: Callable[[str], int]) -> list[str]:
    """Split Markdown so each piece's rendered size (`measure`, the adapter's) fits
    MAX_MSG — a raw-length-only split can render past the limit and fall back to plain.
    Each piece is a prefix that fits, the longest or one filling FULL_ENOUGH, found by
    bisection, every piece measured as sent. It ends at a newline when one fits (before
    a line indented under 4 spaces when that piece fills HALF_FULL); else inside the
    line at whitespace outside inline formatting, else at whitespace (each only when
    the piece fills HALF_FULL, so never one holding only a block marker), else at the
    longest-fitting character. A code
    block open at the cut is closed at the end of the piece (never right after its
    opening line) and reopened (same fence line) at the start of the next; a list
    continuation opening the next piece loses its indent; a line cut midway continues
    with its block marker escaped, so it reads as text."""
    if not text:
        return ["(empty)"]
    out = []
    while text:
        if len(text) <= MAX_MSG and measure(text) <= MAX_MSG:
            out.append(text)
            break
        cut = _best_cut(text, measure)
        assert cut is not None, "one character renders past MAX_MSG"
        rest = _rest(text, cut)
        assert len(rest) < len(text), "a cut that does not shorten the text"
        out.append(_piece(text, cut))
        text = rest
    return out


def _best_cut(text: str, measure: Callable[[str], int]) -> int | None:
    """The cut for the next piece of `text` (see `md_chunks`); each cut list is built
    only when the ones before it give no cut."""
    limit = min(len(text), MAX_MSG)

    sizes: dict[int, int] = {}  # a cut's piece size, measured once

    def size_of(cut: int) -> int:
        if cut not in sizes:
            sizes[cut] = measure(_piece(text, cut))
        return sizes[cut]

    openers = _opening_fence_ends(text, limit)
    newlines = [i for i in range(1, limit) if text[i] == "\n" and i not in openers]
    flush = [i for i in newlines if len(_NEXT_INDENT.match(text, i).group(1)) < CODE_INDENT]
    cut = _longest_fit(flush, size_of)
    if cut is not None and size_of(cut) >= HALF_FULL:
        return cut
    cut = _longest_fit(newlines, size_of)
    if cut is not None:
        return cut
    # inside the line after the last cut that may not be taken (an opening fence line)
    first = newlines[0] if newlines else limit
    start = max((i + 1 for i in openers if i < first), default=0)
    line_end = text.find("\n", start)
    line = text[start : len(text) if line_end == -1 else line_end]  # whole, for closing marks
    room = limit - start  # cuts stay within the message
    for cuts in (
        lambda: [start + i for i in _plain_spaces(line) if 0 < i < room],
        lambda: [start + i for i in range(1, min(len(line), room)) if line[i] in " \t"],
    ):
        cut = _longest_fit(cuts(), size_of)
        if cut is not None and size_of(cut) >= HALF_FULL:
            return cut
    return _longest_fit(range(max(start, 1), limit + 1), size_of)


CODE_INDENT = 4  # a line indented this far after a blank line is an indented code block
_NEXT_INDENT = re.compile(r"\n*( *)")  # from a newline: the next nonblank line's indent
_FENCE = re.compile(r"((?: *> ?)* *)(`{3,}|~{3,})([^\n]*)")  # prefix (indent, quotes), run, info
_BLOCK_MARKER = re.compile(r"#{1,6}(?=\s|$)|>|[-+*](?=\s|$)|`{3,}|~{3,}|(?:[-*_] *){3,}$")
_ORDERED_MARKER = re.compile(r"\d{1,9}(?=[.)](?:\s|$))")
_LIST_ITEM = re.compile(r" *(?:[-+*]|\d{1,9}[.)])(?:[ \t]|$)")


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _open_fence(text: str, cut: int) -> str | None:
    """The opening line (with its indent and quote marks) of the fenced code block open
    at `cut` (None outside one); a line `cut` falls inside counts from its start."""
    opener = None
    lines = text[:cut].split("\n")
    if cut < len(text) and text[cut] != "\n":
        lines.pop()  # the line the cut falls inside
    for line in lines:
        match = _FENCE.fullmatch(line)
        if match is None:
            continue
        _, run, info = match.groups()
        if opener is None:
            if not (run[0] == "`" and "`" in info):
                opener = line
        elif run[0] == _fence_run(opener)[0] and len(run) >= len(_fence_run(opener)):
            if not info.strip():
                opener = None
    return opener


def _opening_fence_ends(text: str, limit: int) -> set[int]:
    """The newlines before `limit` that end a line opening a code block (a piece may not
    end there: it would hold only the fence line)."""
    ends = set()
    opener = None
    at = 0
    while at < limit:
        newline = text.find("\n", at)
        if newline == -1:
            break
        match = _FENCE.fullmatch(text[at:newline])
        if match is not None:
            _, run, info = match.groups()
            if opener is None and not (run[0] == "`" and "`" in info):
                opener = run
                if newline < limit:
                    ends.add(newline)
            elif opener is not None and run[0] == opener[0] and len(run) >= len(opener):
                if not info.strip():
                    opener = None
        at = newline + 1
    return ends


def _fence_run(opener: str) -> str:
    return _FENCE.fullmatch(opener).group(2)


def _fence_prefix(opener: str) -> str:
    return _FENCE.fullmatch(opener).group(1)


def _piece(text: str, cut: int) -> str:
    """The piece ending at `cut`, a code block open there closed."""
    opener = _open_fence(text, cut)
    if opener is None:
        return text[:cut]
    return f"{text[:cut]}\n{_fence_prefix(opener)}{_fence_run(opener)}"


def _rest(text: str, cut: int) -> str:
    """The text after the piece ending at `cut`: a code block open there reopened, a
    list continuation unindented, a line cut midway continued with its block marker
    escaped."""
    opener = _open_fence(text, cut)
    at_newline = text[cut] == "\n"
    if opener is not None:
        continued = text[cut + 1 :] if at_newline else _fence_prefix(opener) + text[cut:]
        rest = f"{opener}\n{continued}"
        return _unindent_continuation(text[:cut], rest)
    if at_newline:
        return _unindent_continuation(text[:cut], text[cut:].lstrip("\n"))
    rest = text[cut:].lstrip(" \t")
    if _BLOCK_MARKER.match(rest):
        return "\\" + rest
    ordered = _ORDERED_MARKER.match(rest)
    return f"{rest[: ordered.end()]}\\{rest[ordered.end() :]}" if ordered else rest


def _unindent_continuation(before: str, rest: str) -> str:
    """`rest` with the indent of its leading lines removed when they continue a list
    item in `before` (indented CODE_INDENT or more they would open a code block); the
    lines from the first one indented less on are kept as written."""
    depth = _indent(rest)
    if depth < CODE_INDENT:
        return rest
    parent = next(
        (line for line in reversed(before.split("\n")) if line.strip() and _indent(line) < depth),
        "",
    )
    if not _LIST_ITEM.match(parent):
        return rest  # an indented code block stays one
    lines = rest.split("\n")
    for index, line in enumerate(lines):
        if line.strip() and _indent(line) < depth:
            break
        lines[index] = line[depth:]
    return "\n".join(lines)


def _closes_later(line: str, mark: str, start: int) -> bool:
    """Whether an unescaped `mark` appears in `line` from `start` on."""
    at = line.find(mark, start)
    while at != -1:
        escapes = len(line[:at]) - len(line[:at].rstrip("\\"))
        if escapes % 2 == 0:
            return True
        at = line.find(mark, at + 1)
    return False


def _plain_spaces(line: str) -> list[int]:
    """The positions of whitespace in one line of Markdown outside a code span, a `$`
    math span, a `**` / `__` run and a link — each counted open only when its closing
    mark follows later on the line."""
    cuts = []
    code = 0  # length of the open code span's backtick run
    math = strong = ""  # the open run's mark ("" when none is open)
    in_destination = False
    brackets = 0
    i = 0
    while i < len(line):
        char = line[i]
        if char == "\\" and not code:
            i += 2
            continue
        if char == "`":
            run = len(line[i:]) - len(line[i:].lstrip("`"))
            if code == run:
                code = 0
            elif not code and _closes_later(line, "`" * run, i + run):
                code = run
            i += run
            continue
        if code:
            i += 1
            continue
        if char == "$":
            math = "" if math else ("$" if _closes_later(line, "$", i + 1) else "")
        elif not math and line.startswith(("**", "__"), i):
            mark = line[i : i + 2]
            if strong == mark:
                strong = ""
            elif not strong and _closes_later(line, mark, i + 2):
                strong = mark
            i += 2
            continue
        elif not math and char == "[" and _closes_later(line, "]", i + 1):
            brackets += 1
        elif not math and char == "]" and brackets:
            brackets -= 1
            in_destination = line.startswith("(", i + 1) and _closes_later(line, ")", i + 2)
        elif in_destination and char == ")":
            in_destination = False
        elif char in " \t" and not (math or strong or brackets or in_destination):
            cuts.append(i)
        i += 1
    return cuts


def _longest_fit(cuts, size_of: Callable[[int], int]) -> int | None:
    """The largest of the ascending `cuts` whose piece (`size_of`) measures within
    MAX_MSG, or a fitting one measuring at least FULL_ENOUGH, or None when none fits.
    The longest is tried first, then the bracket is narrowed by bisection, each probe
    placed where the sizes measured so far put the limit (sizes grow about linearly
    with the prefix) and kept off the bracket's ends."""
    if not cuts:
        return None
    top = size_of(cuts[-1])
    if top <= MAX_MSG:
        return cuts[-1]
    # cuts[low] fits (-1: none known, size 0), cuts[high] does not
    low, high, low_size, high_size = -1, len(cuts) - 1, 0, top
    while high - low > 1:
        low_at = cuts[low] if low >= 0 else 0
        share = ((MAX_MSG + FULL_ENOUGH) / 2 - low_size) / max(high_size - low_size, 1)
        target = low_at + share * (cuts[high] - low_at)
        middle = _bisect_left(cuts, target, low + 1, high)
        quarter = max((high - low) // 4, 1)
        middle = min(max(middle, low + quarter), high - quarter)
        size = size_of(cuts[middle])
        if size <= MAX_MSG:
            if size >= FULL_ENOUGH:
                return cuts[middle]
            low, low_size = middle, size
        else:
            high, high_size = middle, size
    return cuts[low] if low >= 0 else None


def _bisect_left(cuts, value: float, low: int, high: int) -> int:
    """The first index in `cuts[low:high]` at or past `value` (`high` when none)."""
    while low < high:
        middle = (low + high) // 2
        if cuts[middle] < value:
            low = middle + 1
        else:
            high = middle
    return low


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
