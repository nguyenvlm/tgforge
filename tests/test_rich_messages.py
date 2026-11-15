"""Every Telegram message goes out as a Rich Message: Markdown renders to Rich HTML
(math as `<tg-math>`, `<sub>`/`<sup>` as sub/superscripts), plain text as escaped Rich
HTML; sends, edits, card pages, buttons and frames take the rich methods; sizes are
measured against the Rich Message limits; a refused rich body falls back to MarkdownV2,
then plain, in issue order."""

from __future__ import annotations

import asyncio
import contextlib
import html
import logging
import re
import signal

import pytest
from aiogram.exceptions import TelegramBadRequest

from tgforge.base import ui
from tgforge.base import ui as ui_module
from tgforge.base.telegram import (
    RICH_COLUMN_LIMIT,
    RICH_DEPTH_LIMIT,
    MarkdownV2,
    RichMessage,
    TelegramAdapter,
    _page,
    _rich_page,
    rich_blocks,
    rich_columns,
    rich_depth,
    rich_fits,
    rich_html,
)
from tgforge.base.ui import FULL_ENOUGH, HALF_FULL, MAX_MSG, md_chunks
from tgforge.testing import MockBot, rich_text, telegram_transport

WINDOW = "7"


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


def test_render_carries_markdown_v2_as_the_fallback():
    rich, plain = TelegramAdapter(None, None).render("**bold** $x$")
    assert isinstance(rich, RichMessage)
    assert rich.html == "<p><b>bold</b> <tg-math>x</tg-math></p>"
    assert isinstance(rich.fallback, MarkdownV2) and rich.fallback.startswith("*bold*")
    assert plain == "**bold** $x$"


def test_a_render_over_the_rich_block_limit_goes_markdown_v2():
    rich, _ = TelegramAdapter(None, None).render("- a\n" * 600)  # 601 blocks, short text
    assert isinstance(rich, MarkdownV2)


def test_plain_text_goes_rich_first_then_plain():
    rich, plain = TelegramAdapter.bodies("a < b\nc")
    assert rich == RichMessage("<p>a &lt; b<br/>c</p>", None)
    assert plain == "a < b\nc" and not isinstance(plain, RichMessage)
    assert TelegramAdapter.bodies(plain) == [plain]  # the plain body is the last


# ── Sizes ──────────────────────────────────────────────────────────


def test_measure_counts_the_rich_block_limit():
    text = "- a\n" * 300  # 301 blocks: 60% of the block limit, far under MAX_MSG chars
    assert TelegramAdapter.measure(text) >= MAX_MSG * 301 // 500


def test_a_list_past_the_block_limit_splits_into_messages_that_fit():
    text = "\n".join(f"- {i}" for i in range(900))
    pieces = md_chunks(text, TelegramAdapter.measure)
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

    pieces = md_chunks("- a\n" * 3000, measure)
    assert len(calls) <= 4 * len(pieces)  # bisection, not one newline at a time


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
    """Yields to other tasks on every call; refuses a rich body holding `refuse`."""

    def __init__(self, refuse):
        super().__init__()
        self.refuse = refuse

    async def send_rich_message(self, chat_id, rich_message, **kw):
        await asyncio.sleep(0.01)
        if self.refuse in rich_message.html:
            raise _Refused()
        return await super().send_rich_message(chat_id, rich_message, **kw)

    async def send_message(self, chat_id, text, **kw):
        await asyncio.sleep(0.01)
        return await super().send_message(chat_id, text, **kw)


def test_a_refused_send_keeps_issue_order_against_a_concurrent_send():
    async def scenario():
        bot = _YieldingBot(refuse="first")
        tx = telegram_transport(bot)
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
    text = "~~~\n" + "".join(f"line {i}\n\n\n" for i in range(1500)) + "~~~\nAfter"
    pieces = md_chunks(text, TelegramAdapter.measure)
    assert len(pieces) > 1
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
    pieces = md_chunks(text, TelegramAdapter.measure)
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
    pieces = md_chunks(text, TelegramAdapter.measure)
    assert len(pieces) > 1
    assert " ".join(pieces).split() == text.split()
    for piece in pieces:
        assert piece.count("**") % 2 == 0 and piece.count("`") % 2 == 0
        assert piece.count("[") == piece.count("](https://e.com)")


def test_pieces_are_packed_close_to_full():
    paragraph = "Some **bold** text with `code`, a [link](https://x.y), $x^2$ and F<sub>1</sub>."
    answer = "\n\n".join(f"## Part {i}\n\n{paragraph}\n\n- one\n- two" for i in range(150))
    pieces = md_chunks(answer, TelegramAdapter.measure)
    assert len(pieces) > 2
    assert all(TelegramAdapter.measure(piece) >= FULL_ENOUGH for piece in pieces[:-1])


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
    assert "<tg-math>y</tg-math>" in sent[0] and "<sub>2" in sent[0]


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
        pieces = md_chunks(before + line + after, TelegramAdapter.measure)
    assert all(TelegramAdapter.measure(piece) <= MAX_MSG for piece in pieces)
    rich = [rich_html(piece) for piece in pieces]
    assert line.strip() in "".join(_code(r) for r in rich)  # the code line back, in order
    assert not any(r.endswith("<pre></pre>") or "<pre></pre>" in r for r in rich)


def test_every_cut_shortens_the_text_or_the_split_fails_loud(monkeypatch):
    monkeypatch.setattr(ui_module, "_rest", lambda text, cut: text)
    with _time_limit(5), pytest.raises(AssertionError, match="does not shorten"):
        md_chunks("x\n" * 3000, TelegramAdapter.measure)


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
]


@pytest.mark.parametrize("text", INDENTED_ANSWERS)
def test_no_piece_opens_as_an_indented_code_block(text):
    pieces = md_chunks(text, TelegramAdapter.measure)
    assert len(pieces) > 1
    assert not any(_opens_unintended_code(piece) for piece in pieces[1:])
    assert " ".join(pieces).split() == text.split()  # every word, in order


def test_a_cut_prefers_a_line_that_is_not_indented():
    pieces = md_chunks(INDENTED_ANSWERS[0], TelegramAdapter.measure)
    assert len(pieces) > 1
    assert all(piece.startswith("- **x") for piece in pieces[1:])  # a top-level item each


def test_a_code_block_inside_a_list_item_cut_across_messages_stays_code():
    lines = "".join(f"    echo step {i}\n" for i in range(800))
    pieces = md_chunks(f"1. Run:\n\n    ```sh\n{lines}    ```\n\n2. Done", TelegramAdapter.measure)
    assert len(pieces) > 1
    rich = [rich_html(piece) for piece in pieces]
    code = "\n".join(_code(r) for r in rich)
    assert [f"echo step {i}" for i in range(800)] == [
        ln.strip() for ln in code.split("\n") if ln.strip()
    ]
    assert "```" not in "".join(re.sub(r"<pre>.*?</pre>", "", r, flags=re.S) for r in rich)
    assert "Done" in rich[-1]


def test_an_indented_code_block_cut_across_messages_stays_code():
    lines = "".join(f"    value_{i} = {i}\n" for i in range(800))
    pieces = md_chunks(f"Intro paragraph.\n\n{lines}", TelegramAdapter.measure)
    assert len(pieces) > 1
    assert all(rich_html(piece).startswith("<pre>") for piece in pieces[1:])


def test_a_price_before_a_long_line_keeps_the_pieces_full():
    spans = ["**bold text**", "[a link](https://e.com)", "`code span`", "plain", "words"]
    text = "The plan costs $5 per month. " + " ".join(spans * 300)
    pieces = md_chunks(text, TelegramAdapter.measure)
    assert len(pieces) > 2
    assert all(TelegramAdapter.measure(piece) >= FULL_ENOUGH for piece in pieces[:-1])
    for piece in pieces:  # still cut outside formatting
        assert piece.count("**") % 2 == 0 and piece.count("`") % 2 == 0


@pytest.mark.parametrize("marker", ["> ", "- ", "# ", "1. ", "> - "])
def test_no_piece_holds_only_a_block_marker(marker):
    text = marker + "q" * 6000
    pieces = md_chunks(text, TelegramAdapter.measure)
    assert len(pieces) > 1
    assert all(rich_text(rich_html(piece)).strip() for piece in pieces)
    assert "".join(rich_text(rich_html(piece)) for piece in pieces).count("q") == 6000


@pytest.mark.parametrize("marker", ["> ", "- ", "1. "])
def test_a_long_run_of_block_markers_splits_and_keeps_its_text(marker):
    text = marker * 1200 + "q" * 6000
    with _time_limit(5):
        pieces = md_chunks(text, TelegramAdapter.measure)
    assert all(TelegramAdapter.measure(piece) <= MAX_MSG for piece in pieces)
    assert "".join(pieces).count("q") == 6000


def test_a_shorter_fence_inside_a_longer_one_stays_code():
    inner = "".join(f"```\nline {i}\n```\n" for i in range(600))
    pieces = md_chunks(f"````md\n{inner}````\nAfter", TelegramAdapter.measure)
    assert len(pieces) > 1
    rich = [rich_html(piece) for piece in pieces]
    assert all(r.startswith('<pre><code class="language-md">') for r in rich)
    code = "\n".join(_code(r) for r in rich)
    assert code.count("```") == 1200 and all(f"line {i}" in code for i in range(600))
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
    ],
)
def test_the_cut_scan_counts_a_span_only_when_it_closes(line, cut_after):
    assert [line[:cut].split()[-1] for cut in ui_module._plain_spaces(line)] == cut_after


def test_whitespace_cuts_are_built_only_when_no_newline_cut_fits(monkeypatch):
    calls = []
    scan = ui_module._plain_spaces
    monkeypatch.setattr(ui_module, "_plain_spaces", lambda line: calls.append(line) or scan(line))
    md_chunks("".join(f"{_sentence(i)}\n" for i in range(600)), TelegramAdapter.measure)
    assert calls == []
    md_chunks("word " * 2000, TelegramAdapter.measure)
    assert calls


def test_a_whitespace_cut_fills_half_a_message():
    pieces = md_chunks('```json\n{"k": "' + "a" * 20000 + '"}\n```', TelegramAdapter.measure)
    assert all(TelegramAdapter.measure(piece) >= HALF_FULL for piece in pieces[:-1])
