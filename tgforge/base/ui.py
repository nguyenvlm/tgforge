"""Base UI: platform-neutral text helpers and button components.

Plugins compose standard Markdown, `Pages` and neutral `Button` / `Keyboard` values;
a window's adapter renders them for its platform and supplies the size measures the
splitters here pack against. Also here: chunking, durations, shell views. A `cta`
button carries a cross-app action id so a button shown by one app can trigger a
function another app registered — the bot core routes it.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import AsyncIterator, Callable, Generator
from dataclasses import dataclass

MAX_MSG = 4096
YIELD_EVERY = 0.005  # seconds a lazy cut search holds the event loop before letting it run
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


def md_chunks(text: str, measure: Callable[[str], int], window: int = MAX_MSG) -> list[str]:
    """Split Markdown so each piece's size (`measure`, the adapter's: its largest form)
    fits MAX_MSG; a piece spans at most `window` characters of `text` (the adapter's
    `max_message`). Each piece is the longest prefix that fits, found by bisection over
    the cuts (`_longest_fit`), every piece measured as sent. It ends at a newline when
    one fits (before a line indented under 4 spaces when that piece fills HALF_FULL);
    else inside the line at whitespace outside inline formatting, else at whitespace
    (each only when the piece fills HALF_FULL, so never one holding only a block
    marker), else at the longest-fitting character. A code block open at the cut is
    closed at the end of the piece (never right after its opening line) and reopened
    (same fence line) at the start of the next; a list continuation opening the next
    piece loses its indent; a line cut midway continues with its block marker escaped,
    so it reads as text."""
    if not text:
        return ["(empty)"]
    out = []
    while text:
        piece, text = _split_at(text, _drive(_best_cut(text, window), measure))
        out.append(piece)
    return out


async def md_pieces(
    text: str, measure: Callable[[str], int], window: int = MAX_MSG
) -> AsyncIterator[str]:
    """The pieces of `md_chunks`, one at a time: each cut is searched only when the
    piece before it is taken, and a search lets the event loop run whenever it has held
    it YIELD_EVERY seconds."""
    if not text:
        yield "(empty)"
        return
    while text:
        piece, text = _split_at(text, await _drive_async(_best_cut(text, window), measure))
        yield piece


Search = Generator[str, int, "int | None"]  # yields pieces to measure, receives their sizes


def _drive(search: Search, measure: Callable[[str], int]) -> int | None:
    """Run a cut search, measuring each piece it asks for; its cut."""
    try:
        piece = next(search)
        while True:
            piece = search.send(measure(piece))
    except StopIteration as done:
        return done.value


async def _drive_async(search: Search, measure: Callable[[str], int]) -> int | None:
    """`_drive`, letting the event loop run between measures, and before handing back
    its cut, once the search has held it YIELD_EVERY seconds."""
    held_since = time.perf_counter()
    try:
        piece = next(search)
        while True:
            if time.perf_counter() - held_since > YIELD_EVERY:
                await asyncio.sleep(0)
                held_since = time.perf_counter()
            piece = search.send(measure(piece))
    except StopIteration as done:
        if time.perf_counter() - held_since > YIELD_EVERY:
            await asyncio.sleep(0)  # the caller sends the piece next: not on this hold
        return done.value


def _split_at(text: str, cut: int | None) -> tuple[str, str]:
    """The piece of `text` ending at `cut` and the text after it."""
    assert cut is not None, "one character renders past MAX_MSG"
    if cut >= len(text):
        return text, ""
    rest = _rest(text, cut)
    assert len(rest) < len(text), "a cut that does not shorten the text"
    return _piece(text, cut), rest


def _best_cut(text: str, window: int) -> Search:
    """The cut for the next piece of `text` (see `md_chunks`), as a search that yields
    each piece to measure; each cut list is built only when the ones before it give no
    cut. The text's end is a cut when it lies within `window`."""
    limit = min(len(text), window)
    sizes: dict[int, int] = {}  # a cut's piece size, measured once

    def size_of(cut: int):
        if cut not in sizes:
            sizes[cut] = yield _piece(text, cut)
        return sizes[cut]

    openers = _opening_fence_ends(text, limit)
    excluded = openers | _table_head_ends(text, limit)
    every_newline = [i for i in range(1, limit) if text[i] == "\n"]
    newlines = [i for i in every_newline if i not in excluded]
    if len(text) <= window:
        newlines.append(len(text))
    if len(text) <= window and (yield from size_of(len(text))) <= MAX_MSG:
        return len(text)
    next_indents = _next_indents(text, every_newline)
    flush = [i for i in newlines if next_indents.get(i, 0) < CODE_INDENT]
    cut = yield from _longest_fit(flush, size_of)
    if cut is not None and (cut == newlines[-1] or (yield from size_of(cut)) >= HALF_FULL):
        return cut
    cut = yield from _longest_fit(newlines, size_of)
    if cut is not None:
        return cut
    # inside the line after the last cut that may not be taken (an opening fence line,
    # a table's head)
    first = newlines[0] if newlines else limit
    start = max((i + 1 for i in excluded if i < first), default=0)
    line_end = text.find("\n", start)
    line = text[start : len(text) if line_end == -1 else line_end]  # whole, for closing marks
    room = limit - start  # cuts stay within the message
    for cuts in (
        lambda: [start + i for i in _plain_spaces(line, room) if i > 0],
        lambda: [start + i for i in range(1, min(len(line), room)) if line[i] in " \t"],
    ):
        cut = yield from _longest_fit(cuts(), size_of)
        if cut is not None and (yield from size_of(cut)) >= HALF_FULL:
            return cut
    return (yield from _longest_fit(range(max(start, 1), limit + 1), size_of))


CODE_INDENT = 4  # a line indented this far after a blank line is an indented code block
_NEXT_INDENT = re.compile(r"\n*([ \t]*)")  # from a newline: the next nonblank line's indent
_FENCE = re.compile(r"([ \t>]*)(`{3,}|~{3,})([^\n]*)")  # prefix (indent, quotes), run, info
TAB_STOP = 4  # columns between tab stops (a tab indents to the next one)
# after a container prefix (indent, quote marks); each run of whitespace has one reading
_TABLE_DELIMITER = re.compile(
    r"[ \t>]*(?:\|[ \t]*)?:?-+:?[ \t]*(?:\|[ \t]*:?-+:?[ \t]*)*(?:\|[ \t]*)?"
)
_BLOCK_MARKER = re.compile(r"#{1,6}(?=\s|$)|>|[-+*](?=\s|$)|`{3,}|~{3,}|(?:[-*_] *){3,}$")
_ORDERED_MARKER = re.compile(r"\d{1,9}(?=[.)](?:\s|$))")
_LIST_ITEM = re.compile(r"[ \t]*(?:[-+*]|\d{1,9}[.)])(?:[ \t]|$)")


def _columns(whitespace: str) -> int:
    """The columns leading whitespace spans, a tab reaching the next tab stop."""
    return len(whitespace.expandtabs(TAB_STOP))


def _next_indents(text: str, newlines: list[int]) -> dict[int, int]:
    """For each of the ascending `newlines`, the indent in columns of the next line that
    is not empty (one backward pass: a run of empty lines is read once)."""
    indents: dict[int, int] = {}
    for newline in reversed(newlines):
        following = newline + 1
        if following in indents:  # an empty line: the same next line
            indents[newline] = indents[following]
        else:
            indents[newline] = _columns(_NEXT_INDENT.match(text, newline).group(1))
    return indents


def _indent(line: str) -> int:
    return _columns(line[: len(line) - len(line.lstrip(" \t"))])


def _strip_columns(line: str, columns: int) -> str:
    """`line` without the first `columns` columns of its leading whitespace. The rest is
    kept as written when `columns` ends on a tab stop (a tab past it keeps its width);
    else that whitespace is written as spaces, and so is a tab the cut splits."""
    if columns % TAB_STOP:
        lead = len(line) - len(line.lstrip(" \t"))
        return line[:lead].expandtabs(TAB_STOP)[columns:] + line[lead:]
    width = index = 0
    while index < len(line) and width < columns and line[index] in " \t":
        width = (width // TAB_STOP + 1) * TAB_STOP if line[index] == "\t" else width + 1
        index += 1
    return " " * max(width - columns, 0) + line[index:]


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
    """The piece ending at `cut`, a code block open there closed (the text's end: the
    text as written)."""
    if cut >= len(text):
        return text
    opener = _open_fence(text, cut)
    if opener is None:
        return text[:cut]
    return f"{text[:cut]}\n{_fence_prefix(opener)}{_fence_run(opener)}"


def _rest(text: str, cut: int) -> str:
    """The text after the piece ending at `cut`: a code block open there reopened, a
    list continuation unindented, a table row cut midway continued as a row under the
    table's head, any other line cut midway continued with its block marker escaped."""
    opener = _open_fence(text, cut)
    at_newline = text[cut] == "\n"
    if opener is not None:
        continued = text[cut + 1 :] if at_newline else _fence_prefix(opener) + text[cut:]
        rest = f"{opener}\n{continued}"
        return _unindent_continuation(text[:cut], rest)
    if at_newline:
        head = _table_head(text, cut)
        if head is not None:
            return _unindent_continuation(text[:cut], f"{head}\n{text[cut + 1 :]}")
        return _unindent_continuation(text[:cut], text[cut:].lstrip("\n"))
    rest = text[cut:].lstrip(" \t")
    head = _table_head(text, cut)
    if head is not None:  # inside a body row: its rest is a row under the head
        return f"{head}\n| {rest}"
    if _BLOCK_MARKER.match(rest):
        return "\\" + rest
    ordered = _ORDERED_MARKER.match(rest)
    return f"{rest[: ordered.end()]}\\{rest[ordered.end() :]}" if ordered else rest


def _is_table_row(line: str) -> bool:
    return "|" in line and bool(line.strip())


def _table_head_ends(text: str, limit: int) -> set[int]:
    """The newlines before `limit` that end a table's header or delimiter row — a table's
    first two lines (a piece may not end there: it would hold a table with no rows, or
    half its head)."""
    ends = set()
    previous_row = above_row = False  # the line before is a table row; the one before that
    at = 0
    while at < limit:
        newline = text.find("\n", at)
        line = text[at : len(text) if newline == -1 else newline]
        row = _is_table_row(line)
        if row and previous_row and not above_row and _TABLE_DELIMITER.fullmatch(line):
            ends.add(at - 1)
            if newline != -1 and newline < limit:
                ends.add(newline)
        if newline == -1:
            break
        above_row, previous_row = previous_row, row
        at = newline + 1
    return ends


def _table_head(text: str, cut: int) -> str | None:
    """The header and delimiter rows (the table's first two lines, as written) of the
    table whose body rows a newline `cut` falls between, or inside whose body row a
    `cut` falls; None elsewhere."""
    inside_row = text[cut] != "\n"
    if not inside_row:
        row_start = cut + 1  # the row after the cut
    else:
        row_start = text.rfind("\n", 0, cut) + 1  # the row the cut falls inside
        if row_start == 0:
            return None
        cut = row_start - 1  # from the newline before that row
    next_end = text.find("\n", row_start)
    following = text[row_start : len(text) if next_end == -1 else next_end]
    if not _is_table_row(following):
        return None
    first = end = cut
    while end > 0:  # back to the first line of this run of table rows
        start = text.rfind("\n", 0, end) + 1
        if not _is_table_row(text[start:end]):
            break
        first, end = start, start - 1
    if first >= cut:
        return None
    header_end = text.find("\n", first)
    delimiter_end = text.find("\n", header_end + 1)
    if delimiter_end == -1 or delimiter_end > cut or (delimiter_end == cut and not inside_row):
        return None  # the cut ends the head itself (or falls inside it)
    delimiter = text[header_end + 1 : delimiter_end]
    if not _TABLE_DELIMITER.fullmatch(delimiter):
        return None
    return text[first:delimiter_end]


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
        lines[index] = _strip_columns(line, depth)
    return "\n".join(lines)


_BACKTICK_RUN = re.compile(r"`+")


def _escaped(line: str, at: int) -> bool:
    """Whether the character at `at` follows an odd run of backslashes."""
    before = at - 1
    while before >= 0 and line[before] == "\\":
        before -= 1
    return (at - 1 - before) % 2 == 1


def _last_unescaped(line: str, mark: str) -> int:
    """The position of the last unescaped `mark` in `line`, -1 when there is none."""
    at = line.rfind(mark)
    while at != -1 and _escaped(line, at):
        at = line.rfind(mark, 0, at + len(mark) - 1)
    return at


def _plain_spaces(line: str, end: int | None = None) -> list[int]:
    """The positions before `end` of whitespace in one line of Markdown outside a code
    span, a `$` math span, a `**` / `__` run and a link — each counted open only when
    its closing mark follows later on the line (each mark's last unescaped place found
    once per line, and every backtick run length's last start in one pass over the
    line's runs, so the scan stays linear). A code span closes on the next run of its
    exact length, a backslash before it included (no escapes inside code)."""
    end = len(line) if end is None else min(end, len(line))
    last: dict[str, int] = {}
    runs: dict[int, int] = {}  # a backtick run's length -> its last start

    def closes_later(mark: str, start: int) -> bool:
        if mark[0] == "`":
            if not runs:
                runs.update((m.end() - m.start(), m.start()) for m in _BACKTICK_RUN.finditer(line))
            return runs.get(len(mark), -1) >= start
        if mark not in last:
            last[mark] = _last_unescaped(line, mark)
        return last[mark] >= start

    cuts = []
    code = 0  # length of the open code span's backtick run
    math = strong = ""  # the open run's mark ("" when none is open)
    in_destination = False
    brackets = 0
    i = 0
    while i < end:
        char = line[i]
        if char == "\\" and not code:
            i += 2
            continue
        if char == "`":
            run = _BACKTICK_RUN.match(line, i).end() - i
            if code == run:
                code = 0
            elif not code and closes_later("`" * run, i + run):
                code = run
            i += run
            continue
        if code:
            i += 1
            continue
        if char == "$":
            math = "" if math else ("$" if closes_later("$", i + 1) else "")
        elif not math and line.startswith(("**", "__"), i):
            mark = line[i : i + 2]
            if strong == mark:
                strong = ""
            elif not strong and closes_later(mark, i + 2):
                strong = mark
            i += 2
            continue
        elif not math and char == "[" and closes_later("]", i + 1):
            brackets += 1
        elif not math and char == "]" and brackets:
            brackets -= 1
            in_destination = line.startswith("(", i + 1) and closes_later(")", i + 2)
        elif in_destination and char == ")":
            in_destination = False
        elif char in " \t" and not (math or strong or brackets or in_destination):
            cuts.append(i)
        i += 1
    return cuts


def _longest_fit(cuts, size_of) -> Search:
    """The largest of the ascending `cuts` whose piece (`size_of`, a search step giving
    its size) measures within MAX_MSG: the last cut when it fits, else found by bisection
    (sizes taken to grow with the cut); None when none fits."""
    if not cuts:
        return None
    if (yield from size_of(cuts[-1])) <= MAX_MSG:
        return cuts[-1]
    low, high = -1, len(cuts) - 1  # cuts[low] fits (-1: none known); cuts[high] does not
    while high - low > 1:
        middle = (low + high) // 2
        if (yield from size_of(cuts[middle])) <= MAX_MSG:
            low = middle
        else:
            high = middle
    return cuts[low] if low >= 0 else None


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


TRUNCATED = "…(truncated)\n"  # opens a shell view whose output's head was cut


def shell_view(cmd: str, buf: list[str], status: str, reserve: int = 0) -> str:
    """Render a shell run: prompt line, tail-truncated merged output, and a
    status footer ('…' while streaming). Bounded to MAX_MSG for one edit, less
    `reserve` characters."""
    body = "".join(buf).strip()
    head = f"$ {cmd}\n"
    footer = f"\n[{status}]" if status else ""
    cap = max(0, MAX_MSG - len(head) - len(footer) - 16 - reserve)
    if len(body) > cap:
        body = TRUNCATED + body[-cap:]
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


def most_that_fits(
    build: Callable[[int], str], most: int, fits: Callable[[str], bool] | None
) -> int:
    """The largest `n` up to `most` whose Markdown `build(n)` passes `fits` (a window's
    check that Markdown goes out as one message in every form, `Topic.fits_one_message`):
    `most` when `fits` is None or passes it (one check), else found by bisection, `fits`
    taken to pass every `n` below one it passes; 0 when none does."""
    if fits is None or fits(build(most)):
        return most
    low, high = 0, most  # build(high) does not fit
    while high - low > 1:
        middle = (low + high) // 2
        if fits(build(middle)):
            low = middle
        else:
            high = middle
    return low


def shell_md(
    cmd: str, buf: list[str], status: str, fits: Callable[[str], bool] | None = None
) -> str:
    """Live view of a shell run as standard Markdown: one code block, the output's tail
    kept to fit a message — and, given `fits` (`most_that_fits`), less of it until
    `fits` passes."""

    def block(kept: int) -> str:
        return code_block(shell_view(cmd, buf, status, reserve=MAX_MSG - kept))

    # the truncation mark is room `shell_view` does not count
    return block(most_that_fits(block, MAX_MSG - len(TRUNCATED), fits))


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
