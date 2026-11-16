"""Every Telegram message goes out as a Rich Message: Markdown renders to Rich HTML
(math as `<tg-math>`, `<sub>`/`<sup>` as sub/superscripts), plain text as escaped Rich
HTML; sends, edits, card pages, buttons and frames take the rich methods; sizes are
measured against the Rich Message limits; a refused rich body falls back to MarkdownV2,
then plain, in issue order."""

from __future__ import annotations

import asyncio
import collections
import contextlib
import gc
import html
import logging
import re
import signal
import time

import pytest
from aiogram.exceptions import TelegramBadRequest

from tgforge.base import telegram as telegram_module
from tgforge.base import ui
from tgforge.base import ui as ui_module
from tgforge.base.slack import SlackAdapter
from tgforge.base.telegram import (
    RICH_COLUMN_LIMIT,
    RICH_DEPTH_LIMIT,
    MarkdownV2,
    PlainText,
    RichMessage,
    TelegramAdapter,
    _page,
    _rich_page,
    rich_blocks,
    rich_columns,
    rich_depth,
    rich_fits,
    rich_html,
    to_md,
)
from tgforge.base.ui import HALF_FULL, MAX_MSG, md_chunks
from tgforge.testing import MockBot, rich_text, telegram_transport

WINDOW = "7"
FULL_ENOUGH = MAX_MSG * 15 // 16  # a packed piece fills at least this much of a message


def _telegram_chunks(text: str) -> list[str]:
    """The Telegram split `send_rich` makes: Rich size measured, Telegram's window."""
    return md_chunks(text, TelegramAdapter.measure, TelegramAdapter.max_message)


class _Refused(TelegramBadRequest):
    def __init__(self, message="Bad Request: can't parse rich message"):
        self.message = message

    def __str__(self):
        return self.message


class _RefusingBot(MockBot):
    """Refuses the forms named in `refuse` ("rich", "MarkdownV2", "plain"); `tried`
    lists the form of every send and edit attempt in call order."""

    def __init__(self, refuse=()):
        super().__init__()
        self.refuse = set(refuse)
        self.tried: list[str] = []
        self._delegating = False  # MockBot records a rich send through `send_message`

    def _check(self, form):
        self.tried.append(form)
        if form in self.refuse:
            raise _Refused()

    async def send_rich_message(self, chat_id, rich_message, **kw):
        self._check("rich")
        self._delegating = True
        try:
            return await super().send_rich_message(chat_id, rich_message, **kw)
        finally:
            self._delegating = False

    async def send_message(self, chat_id, text, **kw):
        if not self._delegating:
            self._check(kw.get("parse_mode") or "plain")
        return await super().send_message(chat_id, text, **kw)

    async def edit_message_text(self, text=None, **kw):
        form = "rich" if kw.get("rich_message") is not None else kw.get("parse_mode") or "plain"
        self._check(form)
        return await super().edit_message_text(text, **kw)


def _run(coro):
    return asyncio.run(coro)


def _only(bot: MockBot) -> tuple[int, str]:
    """The one message sent: its id and its Rich HTML."""
    assert len(bot.created) == 1, bot.created
    message_id = bot.created[0][0]
    return message_id, bot.rich.get(message_id)


# ── Rendering ──────────────────────────────────────────────────────


def test_inline_math_renders_as_tg_math():
    assert rich_html("area $x^2$ here") == "<p>area <tg-math>x^2</tg-math> here</p>"


def test_display_math_renders_as_a_math_block():
    assert rich_html("$$E = mc^2$$") == "<tg-math-block>E = mc^2</tg-math-block>"


def test_sub_and_sup_render_as_rich_sub_and_superscripts():
    assert rich_html("F<sub>1</sub> and H<sup>+</sup>") == (
        "<p>F<sub>1</sub> and H<sup>+</sup></p>"
    )


def test_other_raw_html_and_a_dollar_price_stay_literal_text():
    assert rich_html("a <b>x</b> costs $5 and $10") == (
        "<p>a &lt;b&gt;x&lt;/b&gt; costs $5 and $10</p>"
    )


def test_a_stray_closing_tag_stays_text_and_an_unclosed_one_is_closed_in_order():
    assert rich_html("x</sub> y") == "<p>x&lt;/sub&gt; y</p>"
    # the unclosed <sub> closes before the bold it sits in, so the HTML stays nested
    assert rich_html("**a <sub>b**") == "<p><b>a <sub>b</sub></b></p>"
    assert rich_html("# H<sub>2") == "<h1>H<sub>2</sub></h1>"


def test_render_carries_its_markdown_for_the_fallback():
    rich, plain = TelegramAdapter(None, None).render("**bold** $x$")
    assert rich == RichMessage("<p><b>bold</b> <tg-math>x</tg-math></p>", None, "**bold** $x$")
    forms = list(TelegramAdapter.bodies(rich))
    assert forms[0] == rich and len(forms) == 2  # each form one message
    assert isinstance(forms[1], MarkdownV2) and forms[1].startswith("*bold*")
    assert plain == "**bold** $x$"


def test_markdown_over_one_message_in_one_of_its_forms_gets_no_rich_body():
    adapter = TelegramAdapter(None, None)
    for markdown in ("- a\n" * 600, "a.b " * 1000):  # 601 blocks; MarkdownV2 escapes each dot
        assert len(markdown) <= MAX_MSG
        assert adapter.render(markdown) == (None, markdown)  # the plain body goes instead
    rich, _ = adapter.render("- a\n" * 400)
    assert rich is not None and rich.html is not None


def test_plain_text_goes_rich_first_then_plain():
    rich, plain = TelegramAdapter.bodies("a < b\nc")
    assert rich == RichMessage("<p>a &lt; b<br/>c</p>", None, None)
    assert plain == "a < b\nc" and isinstance(plain, PlainText)
    assert list(TelegramAdapter.bodies(plain)) == [plain]  # the plain body is the last


# ── Sizes ──────────────────────────────────────────────────────────


def test_measure_counts_the_rich_block_limit():
    text = "- a\n" * 300  # 301 blocks: 60% of the block limit, far under MAX_MSG chars
    assert TelegramAdapter.measure(text) >= MAX_MSG * 301 // 500


def test_a_list_past_the_block_limit_splits_into_messages_that_fit():
    text = "\n".join(f"- {i}" for i in range(900))
    pieces = _telegram_chunks(text)
    assert len(pieces) > 1
    assert "".join(p.replace("\n", "") for p in pieces) == text.replace("\n", "")
    for piece in pieces:
        assert rich_fits(rich_html(piece)) and rich_blocks(rich_html(piece)) <= 500


@pytest.mark.parametrize(
    "text, count",
    [("-\n" * 3000, "<li>"), ("- - a\n" * 2000, "a</li>"), ("#\n" * 3000, "<h1>")],
)
def test_block_dense_text_goes_out_rich_within_the_block_limit(text, count):
    async def scenario():
        bot = MockBot()
        tx = telegram_transport(bot, pace_per_min=0, pace_burst=0)
        await tx.send_rich(WINDOW, text)
        ids = [message_id for message_id, _thread, _text in bot.created]
        assert len(ids) > 1 and {bot.forms[i] for i in ids} == {"rich"}
        for i in ids:
            assert rich_fits(bot.rich[i]) and rich_blocks(bot.rich[i]) <= 500
        assert sum(bot.rich[i].count(count) for i in ids) == text.count("\n")  # nothing lost

    _run(scenario())


def test_the_split_finds_each_cut_in_a_few_measures():
    calls = []

    def measure(text):
        calls.append(len(text))
        return TelegramAdapter.measure(text)

    text = "- a\n" * 6000
    pieces = md_chunks(text, measure, TelegramAdapter.max_message)
    assert len(pieces) > 10
    assert len(calls) <= 13 * len(pieces)  # a bisection over a message's cuts, not each cut
    assert sum(calls) <= 12 * len(text)  # each search measures about its own piece, not the text


def test_a_page_at_its_markdown_v2_limit_fits_the_rich_limits():
    # the costliest characters in Rich HTML per MarkdownV2 character: `&` (5 bytes),
    # `<` (4), a 4-byte emoji, a newline (`<br/>`)
    entries = ["&<😀\n&&&&" * 200] * 40
    pages = ui.expandable_pages("🔧 40", entries, TelegramAdapter.page_size)
    assert len(pages.pages) > 1
    for i, body in enumerate(pages.pages):
        head = ui.page_head(pages, i)
        assert len(_page(head, body, i == 0)[0]) <= MAX_MSG
        assert rich_fits(_rich_page(head, body, i == 0))


# ── Limits past the API's nesting and column caps ──────────────────


def test_a_twenty_deep_list_goes_out_rich_with_all_its_text():
    async def scenario():
        bot = MockBot()
        tx = telegram_transport(bot)
        await tx.send_rich(WINDOW, "".join("  " * i + f"- level{i}\n" for i in range(20)))
        message_id, html = _only(bot)
        assert bot.forms[message_id] == "rich"
        assert rich_depth(html) <= RICH_DEPTH_LIMIT and rich_fits(html)
        shown = rich_text(html)
        assert all(f"level{i}" in shown for i in range(20))

    _run(scenario())


def test_a_twenty_deep_quote_goes_out_rich_with_all_its_text():
    async def scenario():
        bot = MockBot()
        tx = telegram_transport(bot)
        await tx.send_rich(WINDOW, "".join(">" * i + f" quote{i}\n\n" for i in range(1, 21)))
        message_id, html = _only(bot)
        assert bot.forms[message_id] == "rich" and rich_depth(html) <= RICH_DEPTH_LIMIT
        shown = rich_text(html)
        assert all(f"quote{i}" in shown for i in range(1, 21))

    _run(scenario())


def test_a_twenty_five_column_table_goes_out_rich_with_all_its_cells():
    async def scenario():
        bot = MockBot()
        tx = telegram_transport(bot)
        row = "|" + "|".join(f"{{}}{i}" for i in range(25)) + "|\n"
        table = row.format(*["h"] * 25) + "|" + "---|" * 25 + "\n" + row.format(*["v"] * 25)
        await tx.send_rich(WINDOW, table)
        message_id, html = _only(bot)
        assert bot.forms[message_id] == "rich"
        assert rich_columns(html) <= RICH_COLUMN_LIMIT and html.count("<table>") == 2
        assert all(f"<th>h{i}</th>" in html and f"<td>v{i}</td>" in html for i in range(25))

    _run(scenario())


def test_rich_fits_checks_nesting_and_columns():
    deep = "<blockquote>" * 17 + "x" + "</blockquote>" * 17
    wide = "<table><tr>" + "<td>x</td>" * 21 + "</tr></table>"
    assert not rich_fits(deep) and not rich_fits(wide)
    assert rich_fits("<blockquote>" * 16 + "x" + "</blockquote>" * 16)


# ── Calls ──────────────────────────────────────────────────────────


def test_markdown_send_is_a_rich_message_with_math_and_subscripts():
    async def scenario():
        bot = MockBot()
        tx = telegram_transport(bot)
        await tx.send_rich(WINDOW, "Force F<sub>1</sub> and $x^2$")
        message_id, html = _only(bot)
        assert bot.forms[message_id] == "rich"
        assert html == "<p>Force F<sub>1</sub> and <tg-math>x^2</tg-math></p>"

    _run(scenario())


def test_plain_send_is_a_rich_message_showing_the_same_text():
    async def scenario():
        bot = MockBot()
        tx = telegram_transport(bot)
        await tx.send(WINDOW, "a < b & $5")
        message_id, html = _only(bot)
        assert bot.forms[message_id] == "rich"
        assert html == "<p>a &lt; b &amp; $5</p>"
        assert bot.sent[0][1] == "a < b & $5"

    _run(scenario())


def test_edit_is_a_rich_edit():
    async def scenario():
        bot = MockBot()
        tx = telegram_transport(bot)
        assert await tx.edit_md(WINDOW, 50, "H<sub>2</sub>O", "H2O")
        assert bot.forms[50] == "rich"
        assert bot.rich[50] == "<p>H<sub>2</sub>O</p>"

    _run(scenario())


def test_a_frame_edit_is_a_rich_edit():
    async def scenario():
        bot = MockBot()
        tx = telegram_transport(bot)
        assert await tx.land_frame(WINDOW, 50, "x2", "$x^2$")
        assert bot.forms[50] == "rich"
        assert bot.rich[50] == "<p><tg-math>x^2</tg-math></p>"

    _run(scenario())


def test_a_card_and_its_collapsed_pages_are_rich_expandable_quotes():
    async def scenario():
        bot = MockBot()
        tx = telegram_transport(bot)
        pages = ui.Pages("🔧 2 · 3s", ["line 1\nline <2>", "line 3"], "")
        await tx.send_md(WINDOW, pages)
        ids = [message_id for message_id, _thread, _text in bot.created]
        assert [bot.forms[i] for i in ids] == ["rich", "rich"]
        assert bot.rich[ids[0]] == (
            "<p>🔧 2 · 3s</p><blockquote expandable>line 1<br/>line &lt;2&gt;</blockquote>"
        )
        assert bot.rich[ids[1]] == ("<blockquote expandable>↳ 2/2<br/><br/>line 3</blockquote>")

    _run(scenario())


def test_a_card_with_a_note_and_no_body():
    pages = ui.Pages("🔧 0", [""], "done")
    rich, _ = TelegramAdapter(None, None).render_pages(pages)[0]
    assert rich.html == "<p>🔧 0</p><p>done</p>"


def test_buttons_ride_the_rich_message_and_a_button_edit_keeps_its_method():
    async def scenario():
        bot = MockBot()
        tx = telegram_transport(bot)
        keys = ui.keyboard([[ui.button("Go", "go")]])
        await tx.send_rich(WINDOW, "pick $n$", reply_markup=keys)
        message_id, html = _only(bot)
        assert bot.forms[message_id] == "rich" and "<tg-math>n</tg-math>" in html
        assert bot.last_markup.inline_keyboard[0][0].callback_data == "go"
        assert await tx.set_buttons(WINDOW, message_id, None)
        assert bot.markup_cleared == [message_id]  # buttons only: editMessageReplyMarkup

    _run(scenario())


def test_a_bottom_frame_is_sent_rich():
    async def scenario():
        bot = MockBot()
        tx = telegram_transport(bot)
        tx.show_bottom_frame(WINDOW, "panel", "rows", "**rows**")
        await asyncio.wait_for(tx.flush_frames(), 2)
        message_id = tx.bottom_frame_id("panel")
        assert bot.forms[message_id] == "rich" and bot.rich[message_id] == "<p><b>rows</b></p>"

    _run(scenario())


# ── Refusal fallback ───────────────────────────────────────────────


def test_a_refused_rich_send_falls_back_to_markdown_v2_and_logs_it(caplog):
    async def scenario():
        bot = _RefusingBot(refuse={"rich"})
        tx = telegram_transport(bot)
        with caplog.at_level(logging.WARNING, logger="tgforge"):
            await tx.send_rich(WINDOW, "**F**<sub>1</sub>")
        assert bot.tried == ["rich", "MarkdownV2"]
        assert [bot.forms[i] for i, _t, _x in bot.created] == ["MarkdownV2"]
        refusal = [
            r.getMessage() for r in caplog.records if "rich message refused" in r.getMessage()
        ]
        size = len(rich_html("**F**<sub>1</sub>").encode())
        assert refusal == [
            f"rich message refused ({size} bytes, 1 blocks): Bad Request: can't parse rich message"
        ]

    _run(scenario())


def test_a_send_refused_rich_and_markdown_v2_lands_plain():
    async def scenario():
        bot = _RefusingBot(refuse={"rich", "MarkdownV2"})
        tx = telegram_transport(bot)
        await tx.send_rich(WINDOW, "**F**")
        # the Markdown body: rich, MarkdownV2; then its plain body: rich, plain
        assert bot.tried == ["rich", "MarkdownV2", "rich", "plain"]
        assert [(bot.forms[i], text) for i, _t, text in bot.created] == [("plain", "**F**")]

    _run(scenario())


def test_a_refused_rich_edit_falls_back_to_markdown_v2():
    async def scenario():
        bot = _RefusingBot(refuse={"rich"})
        tx = telegram_transport(bot)
        assert await tx.edit_md(WINDOW, 50, "**x**", "x")
        assert bot.tried == ["rich", "MarkdownV2"]
        assert bot.forms[50] == "MarkdownV2"

    _run(scenario())


def test_a_refused_rich_frame_edit_falls_back_to_markdown_v2():
    async def scenario():
        bot = _RefusingBot(refuse={"rich"})
        tx = telegram_transport(bot)
        assert await tx.land_frame(WINDOW, 50, "x", "**x**")
        assert bot.tried == ["rich", "MarkdownV2"]
        assert bot.forms[50] == "MarkdownV2"

    _run(scenario())


def test_a_refused_card_page_falls_back_and_pages_stay_in_order():
    async def scenario():
        bot = _RefusingBot(refuse={"rich"})
        tx = telegram_transport(bot)
        await tx.send_md(WINDOW, ui.Pages("🔧 2", ["a", "b"], ""))
        texts = [text for _i, _t, text in bot.created]
        assert texts == ["🔧 2\n**>a||", "**>↳ 2/2\n>\n>b||"]
        assert [bot.forms[i] for i, _t, _x in bot.created] == ["MarkdownV2", "MarkdownV2"]

    _run(scenario())


class _YieldingBot(MockBot):
    """Yields to other tasks on every call; refuses a rich body holding `refuse`, after
    longer than any other call takes whole."""

    def __init__(self, refuse):
        super().__init__()
        self.refuse = refuse

    async def send_rich_message(self, chat_id, rich_message, **kw):
        if self.refuse in rich_message.html:
            await asyncio.sleep(0.05)
            raise _Refused()
        await asyncio.sleep(0.01)
        return await super().send_rich_message(chat_id, rich_message, **kw)

    async def send_message(self, chat_id, text, **kw):
        await asyncio.sleep(0.01)
        return await super().send_message(chat_id, text, **kw)


def test_a_refused_send_keeps_issue_order_against_a_concurrent_send():
    async def scenario():
        bot = _YieldingBot(refuse="first")
        tx = telegram_transport(bot, pace_per_min=0, pace_burst=0)  # only the order decides
        first = asyncio.create_task(tx.send_rich(WINDOW, "first"))
        await asyncio.sleep(0)  # the first send is issued and waits on its rich call
        second = asyncio.create_task(tx.send(WINDOW, "second"))
        await asyncio.gather(first, second)
        texts = [text for _i, _t, text in bot.created]
        assert texts == ["first", "second"]  # the fallback lands before the later send
        assert [bot.forms[i] for i, _t, _x in bot.created] == ["MarkdownV2", "rich"]

    _run(scenario())


# ── Dollar signs: math pairs, prices stay text ─────────────────────


@pytest.mark.parametrize(
    "text, expected",
    [
        (
            "$5 and $\\text{a}_{\\text{b}}$",
            "<p>$5 and <tg-math>\\text{a}_{\\text{b}}</tg-math></p>",
        ),
        ("costs $5 and $7", "<p>costs $5 and $7</p>"),
        ("between $5-$7", "<p>between $5-$7</p>"),
        ("$x^2$", "<p><tg-math>x^2</tg-math></p>"),
        ("$2x+1$", "<p><tg-math>2x+1</tg-math></p>"),
        ("\\$5 and $y$", "<p>$5 and <tg-math>y</tg-math></p>"),
        ("run `echo $5 $x$` now", "<p>run <code>echo $5 $x$</code> now</p>"),
        ("a $$E=mc^2$$ b", "<p>a </p><tg-math-block>E=mc^2</tg-math-block><p> b</p>"),
        # a price, then a symbol whose `$` follows punctuation
        ("The fee ($5) covers ($x$)", "<p>The fee ($5) covers (<tg-math>x</tg-math>)</p>"),
        (
            "Pro ($5/month) adds the field ($\\text{F}_{\\text{x}}$)",
            "<p>Pro ($5/month) adds the field (<tg-math>\\text{F}_{\\text{x}}</tg-math>)</p>",
        ),
        ("$5/$x$", "<p>$5/<tg-math>x</tg-math></p>"),
        ("$5,$x$", "<p>$5,<tg-math>x</tg-math></p>"),
        ("$5-$x$", "<p>$5-<tg-math>x</tg-math></p>"),
        ("$5 (~$x$)", "<p>$5 (~<tg-math>x</tg-math>)</p>"),
        ("Pay $5.$x$ is next", "<p>Pay $5.<tg-math>x</tg-math> is next</p>"),
        ("$5/$\\alpha$", "<p>$5/<tg-math>\\alpha</tg-math></p>"),
        ("$5/${x}$", "<p>$5/<tg-math>{x}</tg-math></p>"),
        ("($5) or ($7) for ($x$)", "<p>($5) or ($7) for (<tg-math>x</tg-math>)</p>"),
        ("$2 + 2 = 4$.", "<p><tg-math>2 + 2 = 4</tg-math>.</p>"),
        ("$2$", "<p><tg-math>2</tg-math></p>"),
        # math opening on a digit and running into a letter stays math
        (
            "the $2$nd term and the $3$rd",
            "<p>the <tg-math>2</tg-math>nd term and the <tg-math>3</tg-math>rd</p>",
        ),
        ("We get $2$x and $3$y.", "<p>We get <tg-math>2</tg-math>x and <tg-math>3</tg-math>y.</p>"),
        (
            "$1$st, $2$nd, $3$rd",
            "<p><tg-math>1</tg-math>st, <tg-math>2</tg-math>nd, <tg-math>3</tg-math>rd</p>",
        ),
        ("Let $2x$y", "<p>Let <tg-math>2x</tg-math>y</p>"),
        ("Rotate by $3\\pi$rad.", "<p>Rotate by <tg-math>3\\pi</tg-math>rad.</p>"),
        ("The range is $10^{3}$m wide.", "<p>The range is <tg-math>10^{3}</tg-math>m wide.</p>"),
        ("speed $10^{8}$m/s", "<p>speed <tg-math>10^{8}</tg-math>m/s</p>"),
        ("$f(2)$x", "<p><tg-math>f(2)</tg-math>x</p>"),
    ],
)
def test_dollar_pairing_on_the_sent_payload(text, expected):
    async def scenario():
        bot = MockBot()
        tx = telegram_transport(bot)
        await tx.send_rich(WINDOW, text)
        _message_id, html = _only(bot)
        assert html == expected

    _run(scenario())


# ── Splitting: code blocks, long lines, packing ────────────────────


def _send_pieces(text: str) -> list[str]:
    """The Rich HTML of each message `send_rich` sends for `text` (all sent rich)."""

    async def scenario():
        bot = MockBot()
        tx = telegram_transport(bot, pace_per_min=0, pace_burst=0)
        await tx.send_rich(WINDOW, text)
        ids = [message_id for message_id, _thread, _text in bot.created]
        assert {bot.forms[i] for i in ids} == {"rich"}
        return [bot.rich[i] for i in ids]

    return asyncio.run(scenario())


def _code(rich: str) -> str:
    return "".join(
        html.unescape(code)
        for code in re.findall(r"<pre>(?:<code[^>]*>)?(.*?)(?:</code>)?</pre>", rich, re.S)
    )


def test_a_code_block_cut_across_messages_stays_code():
    lines = "".join(f"# comment {i} costs $5 and $x$\nvalue_{i} = {i}\n" for i in range(300))
    sent = _send_pieces(f"Intro\n\n```python\n{lines}```\n\nAfter")
    assert len(sent) > 1
    assert all('<pre><code class="language-python">' in rich for rich in sent)
    assert not any("<h1>" in rich or "<tg-math>" in rich for rich in sent)
    assert "\n".join(_code(rich) for rich in sent) == lines.rstrip("\n")  # every line, in order
    assert sent[-1].endswith("<p>After</p>") and sent[0].startswith("<p>Intro</p>")


def test_a_cut_inside_a_tilde_fence_keeps_its_blank_lines():
    text = "~~~\n" + "".join(f"line {i}" + "\n" * 50 for i in range(300)) + "~~~\nAfter"
    pieces = _telegram_chunks(text)
    assert len(pieces) > 1
    assert any(piece.startswith("~~~\n\n") for piece in pieces[1:])  # a cut in a blank run
    assert all(piece.startswith("~~~\n") for piece in pieces)
    assert all(piece.endswith("\n~~~") for piece in pieces[:-1])
    # the pieces without the fence lines added at each cut give back the text exactly
    bodies = [pieces[0].removesuffix("\n~~~")]
    bodies += [piece.removeprefix("~~~\n").removesuffix("\n~~~") for piece in pieces[1:-1]]
    bodies.append(pieces[-1].removeprefix("~~~\n"))
    assert "\n".join(bodies) == text
    assert rich_html(pieces[-1]).endswith("<p>After</p>")


def test_a_code_block_cut_on_a_plain_measure_stays_closed():
    lines = "".join(f"# step {i}\n" for i in range(800))
    pieces = md_chunks(f"```sh\n{lines}```\nAfter", len)
    assert len(pieces) > 1 and all(len(piece) <= MAX_MSG for piece in pieces)
    assert all(piece.startswith("```sh\n") for piece in pieces)
    assert all(piece.rstrip("After").rstrip("\n").endswith("```") for piece in pieces)


def test_a_line_longer_than_a_message_reads_back_whole():
    text = "&<>" * 9000
    sent = _send_pieces(text)
    assert len(sent) > 1
    assert "".join(rich_text(rich) for rich in sent) == text  # every character, no quote
    assert not any("<blockquote" in rich for rich in sent)


def test_a_long_text_line_is_cut_between_words_outside_formatting():
    words = ["improving", "the", "**parameter matrix**", "[a link](https://e.com/x)"]
    words += ["`code span`", "$x_1$", "for", "> #", "-", "1.", "+", "and"]
    text = " ".join(words * 200)
    pieces = _telegram_chunks(text)
    assert len(pieces) > 1
    # every cut falls between words; a piece opening with a block marker has it escaped
    assert " ".join(pieces).replace("\\", "").split() == text.split()
    for piece in pieces:
        assert piece.count("**") % 2 == 0 and piece.count("`") % 2 == 0
        assert piece.count("[a link](https://e.com/x)") == piece.count("[")
        rich = rich_html(piece)
        assert not re.match(r"<(blockquote|h1|ul|ol)", rich), piece[:20]


def test_a_long_line_of_spans_is_cut_between_spans():
    # nearly every space sits inside bold, a link's text or a code span
    spans = [
        "**" + " ".join(["bold"] * 40) + "**",
        "[" + " ".join(["link"] * 40) + "](https://e.com)",
    ]
    spans += ["`" + " ".join(["code"] * 40) + "`"]
    text = " ".join(spans * 40)
    pieces = _telegram_chunks(text)
    assert len(pieces) > 1
    assert " ".join(pieces).split() == text.split()
    for piece in pieces:
        assert piece.count("**") % 2 == 0 and piece.count("`") % 2 == 0
        assert piece.count("[") == piece.count("](https://e.com)")


PARAGRAPH = "Some **bold** text with `code`, a [link](https://x.y), $x^2$ and F<sub>1</sub>."
SHORT_ITEMS = "\n".join("- a" for _ in range(40))


@pytest.mark.parametrize(
    "answer, fill",
    [
        (
            "\n\n".join(f"## Part {i}\n\n{PARAGRAPH}\n\n- one\n- two" for i in range(150)),
            FULL_ENOUGH,
        ),
        # long paragraphs, then block-dense lists: the size grows unevenly with the text
        ("\n\n".join([PARAGRAPH * 20] * 60 + [SHORT_ITEMS] * 60), HALF_FULL),
    ],
)
def test_pieces_are_packed_close_to_full(answer, fill):
    pieces = _telegram_chunks(answer)
    assert len(pieces) > 2
    assert all(TelegramAdapter.measure(piece) >= fill for piece in pieces[:-1])
    for piece, following in zip(pieces, pieces[1:], strict=False):  # the longest that fits
        assert TelegramAdapter.measure(piece + "\n\n" + following.split("\n\n")[0]) > MAX_MSG


# ── Deep nesting with inline formatting ────────────────────────────

HEAVY = "***~~||[**_x_** F<sub>2<sup>3</sup></sub> `c`](https://e.com)||~~*** $y$"


@pytest.mark.parametrize(
    "text",
    [
        ">" * 10 + " " + HEAVY,
        "".join("  " * i + f"- l{i}\n" for i in range(29)) + "  " * 29 + "- " + HEAVY,
        "".join("  " * i + f"- l{i}\n" for i in range(4)) + "  " * 4 + "- " + HEAVY,
        "\n".join(
            ">" * 10 + " " + row for row in ["| a | b |", "|---|---|", "| $y$ | F<sub>2</sub> |"]
        ),
    ],
)
def test_deep_nesting_with_inline_formatting_goes_out_rich(text):
    sent = _send_pieces(text)
    assert len(sent) == 1
    assert rich_depth(sent[0]) <= RICH_DEPTH_LIMIT
    words = re.findall(r"[A-Za-z0-9]+", re.sub(r"<[^>]+>|\(https://e\.com\)", "", text))
    assert "".join(re.findall(r"[A-Za-z0-9]+", rich_text(sent[0]))) == "".join(words)


# ── Splitting: long code lines, indents, block markers ─────────────


@contextlib.contextmanager
def _time_limit(seconds: float):
    """Fail a block still running after `seconds` (a split that never ends)."""

    def expire(_signum, _frame):
        raise TimeoutError(f"still running after {seconds} s")

    previous = signal.signal(signal.SIGALRM, expire)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


LONG_CODE_LINES = [
    ("```json\n", '{"data": "' + "a" * 5000 + '"}', "\n```"),
    ("Here is the payload:\n\n```json\n", '{"data": "' + "a" * 50000 + '"}', "\n```\nAfter"),
    ("```\n", "word " * 1200, "\n```"),
    ("```\na\nb\nc\n", "y" * 5000, "\n```"),
    ("> ```\n> ", "z" * 5000, "\n> ```"),
]


@pytest.mark.parametrize("before, line, after", LONG_CODE_LINES)
def test_a_code_line_longer_than_a_message_splits_and_ends(before, line, after):
    with _time_limit(5):
        pieces = _telegram_chunks(before + line + after)
    assert all(TelegramAdapter.measure(piece) <= MAX_MSG for piece in pieces)
    rich = [rich_html(piece) for piece in pieces]
    assert line.strip() in "".join(_code(r) for r in rich)  # the code line back, in order
    assert not any(r.endswith("<pre></pre>") or "<pre></pre>" in r for r in rich)


def test_every_cut_shortens_the_text_or_the_split_fails_loud(monkeypatch):
    monkeypatch.setattr(ui_module, "_rest", lambda text, cut: text)
    with _time_limit(5), pytest.raises(AssertionError, match="does not shorten"):
        _telegram_chunks("x\n" * 3000)


def _opens_unintended_code(piece: str) -> bool:
    return rich_html(piece).startswith("<pre>") and not piece.lstrip(" ").startswith("```")


def _sentence(i: int, n: int = 12) -> str:
    words = "improving each principles and the reads parameter".split()
    return " ".join(words[(i + k) % len(words)] for k in range(n))


HEAVY_ITEM = "**bold _it ~~st [link **x** F<sub>2</sub>](https://e.com)~~_** `c` $y^2$"
INDENTED_ANSWERS = [
    "".join(
        f"- **x{i}**: {_sentence(i)}\n    - {_sentence(i + 1)}\n    - `{_sentence(i, 3)}`\n"
        for i in range(120)
    ),
    "".join(
        f"{i}. {_sentence(i)}\n    - {_sentence(i + 1)}\n    - {_sentence(i + 2)}\n"
        for i in range(1, 120)
    ),
    "".join(
        f"{i}. {_sentence(i)}\n\n    ```sh\n    make {_sentence(i, 3)}\n    ```\n\n"
        for i in range(1, 90)
    ),
    "".join("  " * i + f"- l{i} {HEAVY_ITEM}\n" for i in range(40)),
    # one item whose sub-list runs past a message: no unindented line to cut before
    "- parent\n" + "".join(f"    - sub {i} {_sentence(i)}\n" for i in range(400)),
]


@pytest.mark.parametrize("text", INDENTED_ANSWERS)
def test_no_piece_opens_as_an_indented_code_block(text):
    pieces = _telegram_chunks(text)
    assert len(pieces) > 1
    assert not any(_opens_unintended_code(piece) for piece in pieces[1:])
    assert " ".join(pieces).split() == text.split()  # every word, in order


def test_a_cut_prefers_a_line_that_is_not_indented():
    pieces = _telegram_chunks(INDENTED_ANSWERS[0])
    assert len(pieces) > 1
    assert all(piece.startswith("- **x") for piece in pieces[1:])  # a top-level item each


def test_a_code_block_inside_a_list_item_cut_across_messages_stays_code():
    steps = 800
    lines = "".join(f"    echo step {i}\n" for i in range(steps))
    pieces = _telegram_chunks(f"1. Run:\n\n    ```sh\n{lines}    ```\n\n2. Done")
    assert len(pieces) > 1
    rich = [rich_html(piece) for piece in pieces]
    code = "\n".join(_code(r) for r in rich)
    assert [f"echo step {i}" for i in range(steps)] == [
        ln.strip() for ln in code.split("\n") if ln.strip()
    ]
    assert "```" not in "".join(re.sub(r"<pre>.*?</pre>", "", r, flags=re.S) for r in rich)
    assert "Done" in rich[-1]


def test_an_indented_code_block_cut_across_messages_stays_code():
    lines = "".join(f"    value_{i} = {i}\n" for i in range(800))
    pieces = _telegram_chunks(f"Intro paragraph.\n\n{lines}")
    assert len(pieces) > 1
    assert all(rich_html(piece).startswith("<pre>") for piece in pieces[1:])


def test_a_price_before_a_long_line_keeps_the_pieces_full():
    spans = ["**bold text**", "[a link](https://e.com)", "`code span`", "plain", "words"]
    text = "The plan costs $5 per month. " + " ".join(spans * 300)
    pieces = _telegram_chunks(text)
    assert len(pieces) > 2
    assert all(TelegramAdapter.measure(piece) >= FULL_ENOUGH for piece in pieces[:-1])
    for piece in pieces:  # still cut outside formatting
        assert piece.count("**") % 2 == 0 and piece.count("`") % 2 == 0


@pytest.mark.parametrize("marker", ["> ", "- ", "# ", "1. ", "> - "])
def test_no_piece_holds_only_a_block_marker(marker):
    length = 6000
    text = marker + "q" * length
    pieces = _telegram_chunks(text)
    assert len(pieces) > 1
    assert all(rich_text(rich_html(piece)).strip() for piece in pieces)
    assert "".join(rich_text(rich_html(piece)) for piece in pieces).count("q") == length


@pytest.mark.parametrize("marker", ["> ", "- ", "1. "])
def test_a_long_run_of_block_markers_splits_and_keeps_its_text(marker):
    length = 6000
    text = marker * 1200 + "q" * length
    with _time_limit(5):
        pieces = _telegram_chunks(text)
    assert len(pieces) > 1
    assert all(TelegramAdapter.measure(piece) <= MAX_MSG for piece in pieces)
    assert "".join(pieces).count("q") == length


def test_a_shorter_fence_inside_a_longer_one_stays_code():
    count = 600
    inner = "".join(f"```\nline {i}\n```\n" for i in range(count))
    pieces = _telegram_chunks(f"````md\n{inner}````\nAfter")
    assert len(pieces) > 1
    rich = [rich_html(piece) for piece in pieces]
    assert all(r.startswith('<pre><code class="language-md">') for r in rich)
    code = "\n".join(_code(r) for r in rich)
    assert code.count("```") == 2 * count and all(f"line {i}" in code for i in range(count))
    assert rich[-1].endswith("<p>After</p>")


@pytest.mark.parametrize(
    "line, cut_after",
    [
        # escaped marks open nothing
        (
            "cost \\$5 to \\$7 and more words $x$ end",
            ["cost", "\\$5", "to", "\\$7", "and", "more", "words", "$x$"],
        ),
        (
            "a \\*\\*not bold\\*\\* b **bold here** c",
            ["a", "\\*\\*not", "bold\\*\\*", "b", "here**"],
        ),
        ("a $5 b c", ["a", "$5", "b"]),  # a lone price opens nothing
        ("a $x y$ b", ["a", "y$"]),
        ("a [link text](https://e.com) b", ["a", "text](https://e.com)"]),
        ("a [no link b", ["a", "[no", "link"]),
        ("a $5 b \\$6 c d", ["a", "$5", "b", "\\$6", "c"]),  # an escaped $ closes nothing
        ("a `x y\\` b c", ["a", "y\\`", "b"]),  # a backslash in code is text: it closes
        ("a ``x` b c`` d e", ["a", "c``", "d"]),  # only a run of its own length closes it
        ("a ``x` b c", ["a", "``x`", "b"]),  # no run of its length later: nothing opens
    ],
)
def test_the_cut_scan_counts_a_span_only_when_it_closes(line, cut_after):
    assert [line[:cut].split()[-1] for cut in ui_module._plain_spaces(line)] == cut_after


def test_whitespace_cuts_are_built_only_when_no_newline_cut_fits(monkeypatch):
    calls = []
    scan = ui_module._plain_spaces
    monkeypatch.setattr(
        ui_module, "_plain_spaces", lambda line, end=None: calls.append(line) or scan(line, end)
    )
    _telegram_chunks("".join(f"{_sentence(i)}\n" for i in range(600)))
    assert calls == []
    _telegram_chunks("word " * 2000)
    assert calls


def test_a_whitespace_cut_fills_half_a_message():
    pieces = _telegram_chunks(
        '```json\n{"k": "' + "a" * 3 * TelegramAdapter.max_message + '"}\n```'
    )
    assert len(pieces) > 2
    assert all(TelegramAdapter.measure(piece) >= HALF_FULL for piece in pieces[:-1])


@pytest.mark.parametrize("depth", [24, 40])
def test_a_deeply_quoted_answer_splits_without_stalling_the_loop(depth):
    line = "> " * depth + "a line of the quoted reply text\n"
    text = line * (TelegramAdapter.max_message // len(line) + 300)

    async def scenario():
        bot = MockBot()
        tx = telegram_transport(bot, pace_per_min=0, pace_burst=0)
        ticks = []

        async def ticker():
            while True:
                ticks.append(time.monotonic())
                await asyncio.sleep(0.01)

        ticking = asyncio.create_task(ticker())
        await asyncio.sleep(0)
        await tx.send_rich(WINDOW, text)
        ticks.append(time.monotonic())
        ticking.cancel()
        return bot, ticks

    with _time_limit(5):
        bot, ticks = asyncio.run(scenario())
    assert len(bot.created) > 1
    assert max(later - earlier for earlier, later in zip(ticks, ticks[1:], strict=False)) < 1.0


def test_tab_indented_sub_bullets_never_open_a_piece_as_code():
    text = "".join(
        f"- t{i} {_sentence(i)}\n\t- {_sentence(i + 1)}\n\t- {_sentence(i + 2)}\n"
        for i in range(110)
    )
    pieces = _telegram_chunks(text)
    assert len(pieces) > 1
    assert not any(_opens_unintended_code(piece) for piece in pieces[1:])
    assert " ".join(pieces).split() == text.split()


def test_a_tab_counts_to_the_next_tab_stop():
    assert ui_module._indent("\t- a") == 4 and ui_module._indent("  \t- a") == 4
    assert ui_module._indent(" \t \t- a") == 8
    assert ui_module._strip_columns("\t\t- a", 4) == "\t- a"  # ends on a tab stop: kept
    assert ui_module._strip_columns("  \t\tx", 2) == " " * 6 + "x"  # off a tab stop: spaces
    assert ui_module._strip_columns("  \t- a", 2) == "  - a"


TABLE_ROWS = 160
TABLE = (
    "Intro\n\n| key | value | note |\n|---|:---:|---|\n"
    + "".join(
        f"| k{i} | value {i} with some words | note number {i} here |\n" for i in range(TABLE_ROWS)
    )
    + "\nAfter"
)


def test_a_table_cut_across_messages_keeps_its_header_on_telegram():
    sent = _send_pieces(TABLE)
    assert len(sent) > 1
    assert all("<table><tr><th>key</th>" in rich for rich in sent)
    rows = [row for rich in sent for row in re.findall(r"<td>(k\d+)</td>", rich)]
    assert rows == [f"k{i}" for i in range(TABLE_ROWS)]  # every row once, in order
    assert sent[-1].endswith("<p>After</p>")


def test_a_table_cut_across_messages_keeps_every_row_on_slack():
    pieces = md_chunks(TABLE, SlackAdapter.measure)
    assert len(pieces) > 1
    rows = [row for piece in pieces for row in re.findall(r"^\| (k\d+) \|", piece, re.M)]
    assert rows == [f"k{i}" for i in range(TABLE_ROWS)]
    assert all(
        piece.startswith("| key | value | note |\n|---|:---:|---|\n") for piece in pieces[1:]
    )


_CELL_WORDS = "the service retries each request with backoff and logs every refusal".split()


def _long_cell_table(cell: int) -> str:
    def description(row: int) -> str:
        return " ".join(f"{_CELL_WORDS[k % len(_CELL_WORDS)]}{row}" for k in range(cell // 9))

    rows = "".join(f"| item{r} | kind{r} | {description(r)} |\n" for r in range(12))
    return "| Name | Kind | Description |\n|---|---|---|\n" + rows


@pytest.mark.parametrize(
    "text",
    [
        _long_cell_table(2500),
        _long_cell_table(1400),
        "| h0 | h1 |\n|---|---|\n"
        + "".join(f"| a{i} | b{i} |\n" for i in range(50))
        + "| big | "
        + " ".join(f"big{i}" for i in range(1200))
        + " |\n"
        + "".join(f"| c{i} | d{i} |\n" for i in range(300)),
    ],
    ids=["2,500-character cells", "1,400-character cells", "one 1,200-word row"],
)
def test_a_row_too_long_for_a_message_with_its_head_keeps_its_words_and_its_table(text):
    pieces = _telegram_chunks(text)
    head = text.split("\n", 2)[:2]
    assert len(pieces) > 1
    assert collections.Counter(re.findall(r"\w+", "\n".join(pieces))) - collections.Counter(
        re.findall(r"\w+", text)
    ) == collections.Counter(
        {word: (len(pieces) - 1) for word in re.findall(r"\w+", "\n".join(head))}
    )  # every word whole; only the head repeats, once per later piece
    for piece in pieces:
        assert piece.split("\n", 2)[:2] == head  # every piece is the table under its head
        assert "<table>" in rich_html(piece) and TelegramAdapter.measure(piece) <= MAX_MSG


def test_a_table_is_never_cut_right_after_its_head():
    head = "| a | b |\n|---|---|\n"
    text = "x" * 3000 + "\n\n" + head + "| " + "y" * 3000 + " | z |\n"
    pieces = md_chunks(text, len)
    assert not any(piece.rstrip("\n").endswith(("|---|---|", "| a | b |")) for piece in pieces)


# ── Fallbacks: one message per form ────────────────────────────────

WIDE_TITLES = [
    f"the {name} of this row, written out in full as a long column title"
    for name in ("key", "value", "note")
]
WIDE_TABLE = (
    "| "
    + " | ".join(WIDE_TITLES)
    + " |\n|---|---|---|\n"
    + "".join(f"| k{i} | v{i} | n{i} |\n" for i in range(200))
)


def _rows(texts: list[str]) -> list[str]:
    return [row for text in texts for row in re.findall(r"\bk(\d+)\b", text)]


@pytest.mark.parametrize(
    "refuse, form", [({"rich"}, "MarkdownV2"), ({"rich", "MarkdownV2"}, "plain")]
)
def test_a_refused_rich_table_arrives_whole_in_the_next_form(refuse, form, caplog):
    async def scenario():
        bot = _RefusingBot(refuse=refuse)
        tx = telegram_transport(bot, pace_per_min=0, pace_burst=0)
        with caplog.at_level(logging.WARNING, logger="tgforge"):
            await tx.send_rich(WINDOW, WIDE_TABLE)
        texts = [text for _i, _t, text in bot.created]
        assert len(texts) > 1  # the splitter's pieces, each one message in this form
        assert {bot.forms[i] for i, _t, _x in bot.created} == {form}
        assert all(len(text) <= MAX_MSG for text in texts)
        assert _rows(texts) == [str(i) for i in range(200)]  # every row once, in order
        assert sum("rich message refused" in r.getMessage() for r in caplog.records) >= 1

    _run(scenario())


# ── Tabs in code, tab-indented fences ──────────────────────────────

RECIPES = 260


def test_tabs_inside_a_code_block_stay_tabs_when_cut():
    text = (
        "10. Build it:\n\n    ```make\n"
        + "".join(f"    t{i}:\n    \tgcc -o t{i} t{i}.c\n" for i in range(RECIPES))
        + "    ```"
    )
    pieces = _telegram_chunks(text)
    assert len(pieces) > 1
    joined = "\n".join(_code(rich_html(piece)) for piece in pieces)
    assert joined == _code(rich_html(text))  # byte for byte, the uncut code
    slack = md_chunks(text, SlackAdapter.measure)
    assert len(slack) > 1 and sum(piece.count("\tgcc -o") for piece in slack) == RECIPES


@pytest.mark.parametrize("item", ["1. Build it:\n\n", "- Build it:\n"])
def test_a_tab_indented_fence_is_closed_and_reopened(item):
    text = (
        item
        + "\t```make\n"
        + "".join(f"\tt{i}:\n\t\tgcc -o t{i} t{i}.c\n" for i in range(RECIPES))
        + "\t```\n2. done"
    )
    pieces = _telegram_chunks(text)
    assert len(pieces) > 1
    rich = [rich_html(piece) for piece in pieces]
    assert all(
        not r.startswith("<pre>") or piece.lstrip(" \t").startswith("```")
        for piece, r in zip(pieces, rich, strict=True)
    )
    assert "\n".join(_code(r) for r in rich) == _code(rich_html(text))
    assert "```" not in "".join(re.sub(r"<pre>.*?</pre>", "", r, flags=re.S) for r in rich)


# ── Regexes stay linear on adversarial lines ───────────────────────


@pytest.mark.parametrize("run", [" ", "\t"])
def test_a_dash_before_a_long_whitespace_run_does_not_stall_the_loop(run):
    text = "intro\n-" + run * 100000 + "x"

    async def scenario():
        bot = MockBot()
        tx = telegram_transport(bot, pace_per_min=0, pace_burst=0)
        ticks = []

        async def ticker():
            while True:
                ticks.append(time.monotonic())
                await asyncio.sleep(0.01)

        ticking = asyncio.create_task(ticker())
        await asyncio.sleep(0)
        await tx.send_rich(WINDOW, text)
        ticks.append(time.monotonic())
        ticking.cancel()
        return bot, ticks

    with _time_limit(5):
        bot, ticks = asyncio.run(scenario())
    assert bot.created
    assert max(later - earlier for earlier, later in zip(ticks, ticks[1:], strict=False)) < 1.0


ADVERSARIAL = 100000
REGEX_CASES = [
    (ui_module._FENCE.fullmatch, " \t>" * (ADVERSARIAL // 3) + "x"),
    (ui_module._TABLE_DELIMITER.fullmatch, "-" + " " * ADVERSARIAL + "x"),
    (ui_module._TABLE_DELIMITER.fullmatch, "-" + "\t" * ADVERSARIAL + "x"),
    (ui_module._TABLE_DELIMITER.fullmatch, "|" + " " * ADVERSARIAL + "x"),
    (ui_module._TABLE_DELIMITER.fullmatch, " >" * (ADVERSARIAL // 2) + "x"),
    (ui_module._TABLE_DELIMITER.fullmatch, "|-" + " |-" * (ADVERSARIAL // 3) + " x"),
    (ui_module._BLOCK_MARKER.match, "- " * (ADVERSARIAL // 2) + "x"),
    (ui_module._BLOCK_MARKER.match, "-" + " " * ADVERSARIAL + "x"),
    (ui_module._LIST_ITEM.match, " \t" * (ADVERSARIAL // 2) + "x"),
    (lambda text: ui_module._NEXT_INDENT.match(text, 0), "\n" * ADVERSARIAL + " " * ADVERSARIAL),
    (ui_module._ORDERED_MARKER.match, "1" * ADVERSARIAL),
    (rich_depth, "<a" + "b" * ADVERSARIAL),
    (rich_depth, "<a " + "/" * ADVERSARIAL),
    (rich_depth, "<a" * (ADVERSARIAL // 2)),
    (rich_blocks, "<p" * (ADVERSARIAL // 2)),
    (rich_columns, "<tr>" * (ADVERSARIAL // 4)),
    (ui_module._plain_spaces, "$" + "\\$" * (ADVERSARIAL // 2)),
    (ui_module._plain_spaces, "`" + "\\`" * (ADVERSARIAL // 2)),
    (ui_module._plain_spaces, "` " * (ADVERSARIAL // 2)),
    (ui_module._plain_spaces, "$" + "\\$ " * (ADVERSARIAL // 3)),
    (ui_module._plain_spaces, "**" + "\\** " * (ADVERSARIAL // 4)),
]


@pytest.mark.parametrize("call, text", REGEX_CASES)
def test_every_splitter_and_limit_regex_stays_linear(call, text):
    started = time.monotonic()
    with _time_limit(2):
        call(text)
    assert time.monotonic() - started < 1.0


# ── Table heads: the first two lines; tables in quotes and lists ───


def test_a_dash_row_in_a_table_body_is_a_body_row():
    rows = TABLE_ROWS
    text = "| key | value | note |\n|---|---|---|\n" + "".join(
        f"| k{i} | value {i} with some words | note number {i} here |\n"
        + ("|---|---|---|\n" if i == rows // 2 else "")
        for i in range(rows)
    )
    for pieces in (_telegram_chunks(text), md_chunks(text, SlackAdapter.measure)):
        assert len(pieces) > 1
        assert all(piece.startswith("| key | value | note |\n|---|---|---|\n") for piece in pieces)
        assert [r for p in pieces for r in re.findall(r"^\| (k\d+) \|", p, re.M)] == [
            f"k{i}" for i in range(rows)
        ]


@pytest.mark.parametrize(
    "prefix, before", [("> ", "> intro\n>\n"), ("    ", "10. item\n\n"), ("\t", "1. item\n\n")]
)
def test_a_table_in_a_quote_or_list_repeats_its_head(prefix, before):
    rows = TABLE_ROWS
    text = (
        before
        + f"{prefix}| h0 | h1 | h2 |\n{prefix}|---|---|---|\n"
        + "".join(f"{prefix}| r{i} | value {i} words here | note {i} text |\n" for i in range(rows))
    )
    sent = _send_pieces(text)
    assert len(sent) > 1
    assert all("<th>h0</th>" in rich for rich in sent)
    assert [r for rich in sent for r in re.findall(r"<td>(r\d+)</td>", rich)] == [
        f"r{i}" for i in range(rows)
    ]


def test_a_live_message_whose_markdown_v2_is_over_one_message_fails_loud_when_recorded():
    async def scenario():
        bot = _RefusingBot(refuse={"rich"})
        tx = telegram_transport(bot, pace_per_min=0, pace_burst=0)
        assert not tx.fits_one_message(WIDE_TABLE)  # its Rich form fits, its MarkdownV2 not
        for record in (
            lambda: tx.land_frame(WINDOW, 50, WIDE_TABLE[:MAX_MSG], WIDE_TABLE),
            lambda: tx.show_frame(WINDOW, 50, WIDE_TABLE[:MAX_MSG], WIDE_TABLE),
            lambda: tx.show_bottom_frame(WINDOW, "panel", WIDE_TABLE[:MAX_MSG], WIDE_TABLE),
        ):
            with pytest.raises(ValueError, match="over one message"):
                record()
        await asyncio.sleep(0.05)
        assert bot.tried == [] and bot.created == []  # nothing recorded, nothing sent

    _run(scenario())


@pytest.mark.parametrize(
    "marker, block", [("1.", "<ol"), (">", "<blockquote"), ("-", "<ul"), ("#", "<h1")]
)
def test_a_midline_cut_before_a_block_marker_escapes_it(marker, block):
    count = TelegramAdapter.max_message // (len(marker) + 1) + 2000
    text = "a" + f" {marker}" * count  # one line: every space is followed by the marker
    pieces = _telegram_chunks(text)
    assert len(pieces) > 1
    assert not any(rich_html(piece).startswith(block) for piece in pieces[1:])
    assert " ".join(pieces).replace("\\", "").split() == text.split()


def test_a_cut_search_lets_other_tasks_run():
    def search():
        for _ in range(20):
            yield "x"
        return 0

    def measure(piece):
        time.sleep(0.003)  # a measure that holds the loop
        return 1

    async def scenario():
        ran = []

        async def other():
            while True:
                ran.append(time.perf_counter())
                await asyncio.sleep(0)

        task = asyncio.create_task(other())
        await asyncio.sleep(0)
        ran.clear()
        assert await ui_module._drive_async(search(), measure) == 0
        task.cancel()
        return ran

    assert len(asyncio.run(scenario())) >= 5  # the other task ran during the search


def test_a_cut_search_lets_other_tasks_run_before_handing_back_its_cut():
    def search():
        yield "x"
        time.sleep(0.01)  # the search's last step, after its last measure
        return 0

    async def scenario():
        ran = []

        async def other():
            while True:
                ran.append(time.perf_counter())
                await asyncio.sleep(0)

        task = asyncio.create_task(other())
        await asyncio.sleep(0)
        ran.clear()
        cut = await ui_module._drive_async(search(), lambda piece: 1)
        task.cancel()
        return cut, ran

    cut, ran = asyncio.run(scenario())
    assert cut == 0 and ran  # the caller's send of the piece starts on a fresh hold


def test_rows_shaped_like_delimiters_are_cut_between_rows():
    head = "| a |\n|---|\n"
    pieces = md_chunks(head + "|-|\n" * 30000, SlackAdapter.measure)
    assert len(pieces) > 1
    assert all(piece.startswith(head) for piece in pieces)
    assert all(piece.rstrip("\n").endswith("|") for piece in pieces)  # never inside a row
    assert sum(piece.count("|-|") for piece in pieces) == 30000


# ── The event loop runs while a long reply is cut and sent ─────────


class _YieldOnceBot(MockBot):
    """Suspends once per call, as a real request does."""

    async def send_message(self, chat_id, text, **kw):
        await asyncio.sleep(0)
        return await super().send_message(chat_id, text, **kw)


def _fill(unit: str, size: int = 128_000) -> str:
    return (unit * (size // len(unit) + 1))[:size]


def _ladder(unit: str, size: int = 128_000) -> str:
    """`unit` with its `{}` a backtick run of each length from 600 down to 1."""
    return "".join(unit.replace("{}", "`" * k, 1) for k in range(600, 0, -1))[:size]


def _openers_then_escaped(opener: str, closer: str, size: int = 100_000) -> str:
    return _fill(opener, size // 2) + _fill(closer, size // 2)


LOOP_HOLD_LIMIT = 0.1  # seconds
# 17 bold openers, then 17 escaped closers (each leaves a real `*` closing one), per paragraph
DEEP_PER_PARAGRAPH = _fill("**a " * 17 + "\\** " * 17 + "**b** " * 300 + "\n\n")
DEEP_INLINE = [
    DEEP_PER_PARAGRAPH,
    "**a " * 16000 + "\\** " * 16000,
    "<sub>" * 20 + "x" + "</sub>" * 20,
]


def _words(markdown: str) -> list[str]:
    return re.findall(r"[A-Za-z0-9]+", re.sub(r"</?su[bp]>", "", markdown))


@pytest.mark.parametrize("text", DEEP_INLINE, ids=["17 deep per paragraph", "16000 deep", "sub 20"])
def test_inline_nesting_past_the_depth_limit_goes_out_rich_with_every_word(text):
    async def scenario():
        bot = _RefusingBot()
        tx = telegram_transport(bot, pace_per_min=0, pace_burst=0)
        await tx.send_rich(WINDOW, text)
        return bot

    bot = _run(scenario())
    ids = [i for i, _t, _x in bot.created]
    assert set(bot.tried) == {"rich"} and {bot.forms[i] for i in ids} == {"rich"}
    assert all(rich_depth(bot.rich[i]) <= RICH_DEPTH_LIMIT for i in ids)
    assert [w for i in ids for w in _words(rich_text(bot.rich[i]))] == _words(text)


@pytest.mark.parametrize(
    "text",
    [
        *DEEP_INLINE[:2],
        _fill("- - - - a\n", 40_000),
        _fill("> " * 24 + "a line of the quoted reply text\n", 40_000),
        "| a | b | c |\n|---|---|---|\n" + _fill("| 1 | 2 | 3 |\n", 40_000),
        _fill("$x$ a.b_c ", 40_000),
        _fill("Some **bold** `code` [l](https://e.com) F<sub>1</sub> (x)! ", 40_000),
        "```py\n" + _fill("x = '\\\\'  # a.b\n", 40_000) + "```",
        "".join(f"{'  ' * (i % 12)}- item {i} **b** `c`\n" for i in range(1500)),
        WIDE_TABLE * 4,
    ],
    ids=[
        "17 deep",
        "16000 deep",
        "lists",
        "quotes",
        "table",
        "math",
        "inline",
        "code",
        "tree",
        "wide table",
    ],
)
def test_every_piece_is_one_message_in_every_form(text):
    for piece in _telegram_chunks(text):
        rich = rich_html(piece)
        assert len(piece) <= MAX_MSG and len(to_md(piece)) <= MAX_MSG and rich_fits(rich)


# 21 inline levels; inline math, code and a footnote reference innermost
NESTED_20 = "**a _b ~~c " * 7 + "x $m$ `code` y[^n]" + " c~~ b_ a**" * 7


@pytest.mark.parametrize(
    "markdown",
    [
        "# " + NESTED_20,
        "x[^n]\n\n[^n]: " + NESTED_20,
        "| h |\n|---|\n| " + NESTED_20 + " |",
        "> " * 6 + NESTED_20 + " `code` $m$",
        "".join("  " * i + "- l\n" for i in range(7))
        + "\n"
        + "  " * 7
        + "| h |\n"
        + "  " * 7
        + "|---|\n"
        + "  " * 7
        + "| c |\n",
        "".join("  " * i + "- l\n\n" for i in range(8)) + "  " * 8 + "para\n",
        "".join("  " * i + "- l\n" for i in range(7))
        + "\n"
        + "  " * 7
        + "```py\n"
        + "  " * 7
        + "x\n"
        + "  " * 7
        + "```\n",
        "\n".join("> " * 15 + line for line in ("```py", "x", "```")),
        "x[^n]\n\n[^n]: - - - - - - - words\n\n"
        + "\n".join(" " * 18 + line for line in ("```py", "x", "```")),
    ],
    ids=[
        "heading",
        "footnote",
        "table cell",
        "quote",
        "table in list",
        "loose list",
        "code",
        "code in 15 quotes",
        "code in 7 lists in a footnote",
    ],
)
def test_one_walk_fits_the_depth_limit_in_every_context(markdown):
    if "[^n]" in markdown and "[^n]:" not in markdown:
        markdown += "\n\n[^n]: n"  # the reference's definition
    rich = rich_html(markdown)
    assert rich_depth(rich) <= RICH_DEPTH_LIMIT
    source = markdown.replace("[^n]:", "").replace("[^n]", "[n]").replace("```py", "```")
    words = re.findall(r"[a-z]+", source)  # a reference shows as [n]; an info string is no text
    assert "".join(re.findall(r"[a-z]+", rich_text(rich))) == "".join(words)


@pytest.mark.parametrize(
    "text",
    [
        "Note[^1] here.\n\n[^1]: 1. * * * 1. - * > > the quoted words at the bottom",
        "- > - > - - * > 1. _$$x^2$$_ and some words after it",
        "- > - > - - * > 1. _italic $$E=mc^2$$ words_",
    ],
    ids=["list in a footnote", "display math in italics", "display math mid italics"],
)
def test_a_short_deeply_nested_answer_goes_out_as_one_rich_message(text):
    async def scenario():
        bot = _RefusingBot()
        tx = telegram_transport(bot, pace_per_min=0, pace_burst=0)
        await tx.send_rich(WINDOW, text)
        return bot

    bot = _run(scenario())
    ((message_id, _thread, _text),) = bot.created
    assert bot.forms[message_id] == "rich" and rich_depth(bot.rich[message_id]) <= RICH_DEPTH_LIMIT
    words = re.findall(r"[A-Za-z]+", text)  # letters: list numbers render as markers
    assert re.findall(r"[A-Za-z]+", rich_text(bot.rich[message_id])) == words


def test_an_unmatched_spoiler_is_closed_within_its_paragraph():
    # spoiler marks pair differently from the formatting around them: one stays open
    line = (
        "<sup>w16 ||w15 _w14 [w13 **w12 [w11 ~~w10 **w9 <sub>w8 ||w7 ||w6 **w5 ~~w4 ||w3 "
        "<sub>w2 ~~w1 **w0 k z0** z1~~ z2</sub> z3|| z4~~ z5** z6|| z7|| z8</sub> z9** z10~~ "
        "z11](https://e.com) z12** z13](https://e.com) z14_ z15|| z16</sup>"
    )
    rich = rich_html(line + "\n\n" + "".join("  " * i + "- l\n" for i in range(7)))
    first = rich.split("</p>", 1)[0]
    for name in ("tg-spoiler", "sup", "sub", "b", "i"):
        assert first.count(f"<{name}>") == first.count(f"</{name}>"), name
    assert rich_depth(rich) <= RICH_DEPTH_LIMIT


def test_measure_counts_every_form_of_a_piece():
    shorter_in_markdown_v2 = "**b** " * 700  # 4200 characters; MarkdownV2 `*b* ` is 2800
    assert len(to_md(shorter_in_markdown_v2)) < MAX_MSG < len(shorter_in_markdown_v2)
    assert TelegramAdapter.measure(shorter_in_markdown_v2) > MAX_MSG  # its plain text is over
    longer_in_markdown_v2 = "a.b " * 1000  # 4000 characters; MarkdownV2 escapes each dot
    assert TelegramAdapter.measure(longer_in_markdown_v2) > MAX_MSG


@pytest.mark.parametrize(
    "line",
    [
        "".join("`" * k + " a " for k in range(600, 0, -1)),  # backtick runs of falling length
        "".join("\\" + "`" * k + " " for k in range(600, 0, -1)),  # each after a backslash
        "` " * 100_000,
        _openers_then_escaped("[ ", "\\] ", 200_000),
        _openers_then_escaped("$a$ ", "\\$", 200_000),
        _openers_then_escaped("` ` ", "\\`", 200_000),
    ],
    ids=["ladder", "escaped ladder", "backtick spaces", "brackets", "dollars", "backticks"],
)
def test_the_cut_scan_stays_linear_over_a_whole_line(line):
    started = time.monotonic()
    with _time_limit(5):
        ui_module._plain_spaces(line)
    assert time.monotonic() - started < 0.3  # each mark's last place found once per line


def test_a_fitting_window_long_prefix_takes_one_measure_per_piece():
    calls = []

    def measure(text):
        calls.append(len(text))
        return TelegramAdapter.measure(text)

    text = "| a | b | c |\n|---|---|---|\n" + ("| 1 | 2 | 3 |\n" * 9200)[:128000]
    pieces = md_chunks(text, measure, TelegramAdapter.max_message)
    assert len(pieces) > 20 and len(calls) <= 2 * len(pieces)  # no bisection when it fits


def _counting(monkeypatch, name: str) -> list[str]:
    calls: list[str] = []
    real = getattr(telegram_module, name)
    monkeypatch.setattr(
        telegram_module, name, lambda *a, **kw: calls.append(a[0]) or real(*a, **kw)
    )
    for cached in (telegram_module._measure, telegram_module.rich_html):
        cached.cache_clear()
    return calls


def test_a_probe_over_by_its_plain_or_rich_size_skips_the_markdown_v2_render(monkeypatch):
    rendered = _counting(monkeypatch, "to_md")
    assert TelegramAdapter.measure("x" * (MAX_MSG + 1)) > MAX_MSG  # over by its plain length
    assert TelegramAdapter.measure("- a\n" * 1000) > MAX_MSG  # 1,000 blocks: over by Rich size
    assert rendered == []
    assert TelegramAdapter.measure("- a\n" * 100) <= MAX_MSG and len(rendered) == 1


def test_the_send_reuses_the_split_measure_of_its_piece(monkeypatch):
    rendered = _counting(monkeypatch, "to_md")
    piece = "Some **bold** text and $x$\n" * 20
    assert TelegramAdapter.measure(piece) <= MAX_MSG and len(rendered) == 1
    rich, _plain = TelegramAdapter(None, None).render(piece)
    assert rich is not None and len(rendered) == 1  # no second MarkdownV2 render


def test_measure_counts_a_piece_over_the_depth_or_column_limit_as_over(monkeypatch):
    monkeypatch.setattr(telegram_module, "rich_html", lambda text: "<b>" * 17 + "</b>" * 17)
    assert TelegramAdapter.measure("short") > MAX_MSG
    monkeypatch.setattr(telegram_module, "rich_html", lambda text: "<tr>" + "<td>" * 21 + "</tr>")
    assert TelegramAdapter.measure("short") > MAX_MSG


@pytest.mark.parametrize(
    "text",
    [
        _fill("- - - - a\n"),
        _fill("> " * 24 + "a line of the quoted reply text\n"),
        "| a | b | c |\n|---|---|---|\n" + _fill("| 1 | 2 | 3 |\n"),
        _fill("$x$ "),
        "$" + _fill("\\$"),
        "`" + _fill("\\`"),
        _fill("` "),
        "$" + _fill("\\$ "),
        _ladder("`{} a ").ljust(128_000, "a"),  # backtick runs of falling length
        _ladder("\\`{} ").ljust(128_000, "b"),  # each after a backslash
        _openers_then_escaped("[ ", "\\] "),  # openers, then only escaped closers
        _openers_then_escaped("$a$ ", "\\$"),
        _openers_then_escaped("` ` ", "\\`"),
        DEEP_PER_PARAGRAPH,  # inline nesting past 16 levels in every paragraph (R35)
    ],
)
def test_a_long_reply_never_holds_the_event_loop(text):
    async def scenario():
        bot = _YieldOnceBot()
        tx = telegram_transport(bot, pace_per_min=0, pace_burst=0)
        ticks = []

        async def ticker():
            while True:
                ticks.append(time.perf_counter())
                await asyncio.sleep(0.01)

        ticking = asyncio.create_task(ticker())
        await asyncio.sleep(0)
        await tx.send_rich(WINDOW, text)
        ticks.append(time.perf_counter())
        ticking.cancel()
        return bot, ticks

    gc.collect()
    gc.freeze()  # a full collection of the other tests' objects is no hold of the split
    try:
        with _time_limit(10):  # a quadratic scan holds the loop for minutes
            bot, ticks = asyncio.run(scenario())
    finally:
        gc.unfreeze()
    assert len(bot.created) > 1
    assert (
        max(later - earlier for earlier, later in zip(ticks, ticks[1:], strict=False))
        < LOOP_HOLD_LIMIT
    )


def test_a_long_reply_is_cut_one_piece_ahead_of_its_sends():
    text = _fill("- - a\n", 64_000)

    async def scenario():
        bot = _YieldOnceBot()
        tx = telegram_transport(bot, pace_per_min=0, pace_burst=0)
        measured = []
        measure = tx.adapter.measure
        tx.adapter.measure = lambda piece: measured.append(len(piece)) or measure(piece)
        at_first_send = []
        send = bot.send_message

        async def send_message(chat_id, body, **kw):
            if not at_first_send:
                at_first_send.append(sum(measured))
            return await send(chat_id, body, **kw)

        bot.send_message = send_message
        await tx.send_rich(WINDOW, text)
        return at_first_send[0], sum(measured), len(bot.created)

    before_first, total, messages = asyncio.run(scenario())
    assert messages > 10
    assert before_first < total / 4  # two pieces' searches, not the whole reply's


def test_a_long_reply_keeps_its_order_while_another_window_sends():
    text = _fill("- - a\n", 64_000)

    async def scenario():
        bot = _YieldOnceBot()
        tx = telegram_transport(bot, pace_per_min=0, pace_burst=0)
        long_reply = asyncio.create_task(tx.send_rich(WINDOW, text))
        await asyncio.sleep(0.05)
        await tx.send("8", "from another window")
        other_done = len(bot.created)
        await long_reply
        return bot, other_done

    bot, other_done = asyncio.run(scenario())
    reply = [text for _i, thread, text in bot.created if thread == 7]
    assert other_done < len(bot.created)  # the other window did not wait for the reply
    assert "".join(piece.replace("\n", "") for piece in reply).count("a") == text.count("a")


# ── Edits; refusal logs ────────────────────────────────────────────


class _BoldRefusingBot(_RefusingBot):
    """Refuses a rich body holding bold (the Markdown's Rich form) and every MarkdownV2
    body; takes a rich body without it (plain text as escaped Rich HTML)."""

    async def send_rich_message(self, chat_id, rich_message, **kw):
        if "<b>" in rich_message.html:
            self.tried.append("rich")
            raise _Refused()
        return await super().send_rich_message(chat_id, rich_message, **kw)


def test_each_fallback_is_one_message_and_each_refusal_is_logged(caplog):
    async def scenario():
        bot = _BoldRefusingBot(refuse={"MarkdownV2"})
        tx = telegram_transport(bot, pace_per_min=0, pace_burst=0)
        with caplog.at_level(logging.WARNING):
            await tx.send_rich(WINDOW, "Some **bold** text")
        return bot

    bot = _run(scenario())
    assert bot.tried == ["rich", "MarkdownV2", "rich"]  # Rich, MarkdownV2, escaped Rich
    ((message_id, _thread, text),) = bot.created
    assert bot.forms[message_id] == "rich" and text == "Some **bold** text"  # the plain text
    logged = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("rich message refused" in m and "bytes" in m for m in logged)
    assert any("MarkdownV2 message refused" in m and "characters" in m for m in logged)


def test_an_edit_is_one_message_in_every_form():
    async def scenario():
        for refuse, form in (({"rich"}, "MarkdownV2"), ({"rich", "MarkdownV2"}, "plain")):
            bot = _RefusingBot(refuse=refuse)
            tx = telegram_transport(bot, pace_per_min=0, pace_burst=0)
            assert await tx.edit_md(WINDOW, 50, "| a |\n|---|\n" + "| row |\n" * 300, None)
            assert bot.created == [] and bot.forms[50] == form  # edited, nothing sent

    _run(scenario())


class _NotModifiedBot(MockBot):
    """Answers an edit to the text a message already shows "message is not modified"."""

    async def edit_message_text(self, text=None, **kw):
        rich = kw.get("rich_message")
        shown = rich_text(rich.html) if rich is not None else text
        if shown == dict(self.edits).get(kw.get("message_id")):
            raise _Refused("Bad Request: message is not modified")
        return await super().edit_message_text(text, **kw)


def test_a_repeated_edit_with_unchanged_text_changes_nothing():
    async def scenario():
        bot = _NotModifiedBot()
        tx = telegram_transport(bot, pace_per_min=0, pace_burst=0)
        for _ in range(3):
            assert await tx.edit_md(WINDOW, 50, "Some **bold** text and $x$", None)
        assert len(bot.edits) == 1 and bot.created == [] and bot.forms[50] == "rich"

    _run(scenario())


def test_a_refused_markdown_v2_message_is_logged_with_its_size(caplog):
    async def scenario():
        bot = _RefusingBot(refuse={"rich", "MarkdownV2"})
        tx = telegram_transport(bot, pace_per_min=0, pace_burst=0)
        with caplog.at_level(logging.WARNING):
            await tx.send_rich(WINDOW, "Some **bold** text")
            await tx.edit_md(WINDOW, bot.created[0][0], "Other **bold** text", None)
        return bot

    bot = _run(scenario())
    refused = [r for r in caplog.records if "MarkdownV2 message refused" in r.getMessage()]
    assert len(refused) == 2 and all(r.levelno == logging.WARNING for r in refused)
    assert all("characters" in r.getMessage() and "parse" in r.getMessage() for r in refused)
    assert bot.forms[bot.created[0][0]] == "plain"


# ── A live message fits one message when recorded ──────────────────


@pytest.mark.parametrize("record", ["show_frame", "land_frame", "show_bottom_frame"])
def test_an_oversized_live_message_fails_loud_when_recorded(record):
    tx = telegram_transport(MockBot())
    plain = "x" * (MAX_MSG + 1)
    with pytest.raises(ValueError, match="over one message"):
        if record == "show_bottom_frame":
            tx.show_bottom_frame(WINDOW, "panel", plain)
        else:
            getattr(tx, record)(WINDOW, 50, plain)


def test_a_card_of_backslash_heavy_tool_calls_fits_one_message_in_every_form():
    tx = telegram_transport(MockBot())
    entries = ["C:\\\\Users\\\\x\\\\" * 40 + f" call {i}" for i in range(60)]
    pages = ui.expandable_pages("🔧 60", entries, TelegramAdapter.page_size)
    assert len(pages.pages) > 1
    assert tx.fits_one_message(pages)  # what the card's land_frame records


def test_most_that_fits_finds_the_largest_amount():
    assert ui.most_that_fits(lambda n: "x" * n, 100, None) == 100
    assert ui.most_that_fits(lambda n: "x" * n, 100, lambda md: len(md) <= 37) == 37
    assert ui.most_that_fits(lambda n: "x" * n, 100, lambda md: False) == 0


def test_a_shell_view_code_block_fits_one_message_in_every_form():
    tx = telegram_transport(MockBot())
    output = ["\\" * 10 + f" line {i}\n" for i in range(5000)]
    assert not tx.fits_one_message(ui.shell_md("cat log", output, "running"))  # MarkdownV2 over
    view = ui.shell_md("cat log", output, "running", tx.fits_one_message)
    assert len(view) <= MAX_MSG and tx.fits_one_message(view) and "line 4999" in view
    assert len(ui.shell_md("cat log", ["line\n"] * 5000, "running")) <= MAX_MSG
    assert ui.shell_view("cat log", ["line\n"] * 5000, "done").startswith("$ cat log\n…(truncated)")
