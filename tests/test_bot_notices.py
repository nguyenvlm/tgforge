"""Bot notices reach the window's Claude, or the window itself: a fallback rides the next
prompt's [Bot notice(s)] block, a message not delivered or an exception is submitted at
once, repeats are counted, a failure on a reply to a notice prompt never starts another,
and a window with no Claude session shows the notice as one message."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from aiogram.exceptions import TelegramBadRequest
from claude_fakes import client, drain, window

from tgforge.base.kernel import (
    NOTICE_EXCEPTION,
    NOTICE_FALLBACK,
    NOTICE_NOT_DELIVERED,
    Notice,
    Plugin,
    Topic,
    launch,
    on_message,
)
from tgforge.plugins.agent import window as drv
from tgforge.plugins.agent.window import SESSION_BRIEF
from tgforge.plugins.claude.driver import ClaudeTopic
from tgforge.plugins.shell import Shell, ShellTopic
from tgforge.testing import MockBot, TestClient, telegram_transport


class _Refused(TelegramBadRequest):
    def __init__(self):
        super().__init__(method=None, message="Bad Request: can't parse rich message")

    def __str__(self):
        return self.message


class _RefusingBot(MockBot):
    """Refuses the forms named in `refuse` ("rich", "plain"); `tried`
    lists the form of every send attempt."""

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


def _refusing(c, refuse) -> _RefusingBot:
    bot = _RefusingBot(refuse)
    c.bot = bot
    c.adapter.bot = bot
    return bot


def _claude_window(tmp_path, refuse=()):
    c = client(tmp_path)
    bot = _refusing(c, refuse)
    return c, bot, window(c, tmp_path)


async def _send_md(c, t, md="**bold** answer"):
    await t.send_md(md)
    await drain(c, t)
    for _ in range(5):
        await asyncio.sleep(0)


def test_a_refused_rich_send_reaches_claudes_next_prompt_as_a_fallback_notice(tmp_path):
    async def scenario():
        c, bot, t = _claude_window(tmp_path, refuse={"rich"})
        await _send_md(c, t)
        return bot, t, t._build_prompt([])

    bot, t, prompt = asyncio.run(scenario())
    assert bot.tried[:3] == ["rich", "rich", "plain"]
    assert prompt.startswith("[Bot notice(s):\n- fallback: message (Rich → plain)")
    assert "can't parse rich message" in prompt
    assert t.notice_prompts == []  # a fallback waits for the user's next prompt
    assert t._build_prompt([]) == ""  # taken once


def test_every_form_refused_reaches_claude_at_once_as_a_not_delivered_notice(tmp_path):
    async def scenario():
        c, _bot, t = _claude_window(tmp_path, refuse={"rich", "plain"})
        await _send_md(c, t)
        return t

    t = asyncio.run(scenario())
    assert len(t.notice_prompts) == 1
    assert "- not delivered: message (Rich → none)" in t.notice_prompts[0]


def test_a_frame_push_that_raises_raises_an_exception_notice():
    async def edit_message_text(*args, **kw):
        raise RuntimeError("the reply could not be read")

    async def scenario():
        tx = telegram_transport(SimpleNamespace(edit_message_text=edit_message_text))
        notices: list[Notice] = []
        tx.on_notice = notices.append
        await asyncio.wait_for(tx.land_frame("7", 50, "plain"), 5)
        return notices

    notices = asyncio.run(scenario())
    assert [(n.kind, n.window, n.what) for n in notices] == [
        (NOTICE_EXCEPTION, "7", "live message push")
    ]
    assert notices[0].error == "RuntimeError: the reply could not be read"


@launch("/crash", "a window whose handler raises")
class _CrashTopic(Topic):
    id = "crash"

    @on_message
    async def feed(self, ctx):
        raise RuntimeError("boom")


class _Crash(Plugin):
    id = "crash"
    topics = [_CrashTopic]


def test_a_plugin_handler_that_raises_is_shown_in_its_window(tmp_path):
    async def scenario():
        c = TestClient(_Crash(), home=str(tmp_path))
        c.core._instantiate(_CrashTopic, "777", "crash")
        c.core.owners["777"] = "crash"
        await c.send("hello", "777")
        await c.settle()
        for _ in range(5):
            await asyncio.sleep(0)
        return [text for _mid, _thread, text in c.bot.created]

    shown = asyncio.run(scenario())
    assert "⚠️ bot notice — exception: message handler — RuntimeError: boom" in shown


def test_a_stalled_send_step_reaches_claude_as_an_exception_notice(tmp_path, monkeypatch):
    monkeypatch.setattr(drv, "SEND_STEP_STALL", 0.05)

    async def scenario():
        c, _bot, t = _claude_window(tmp_path)
        release = asyncio.Event()
        t._send_in_order(release.wait)

        async def later():
            pass

        step = t._send_in_order(later)
        await asyncio.sleep(0.15)
        release.set()
        await step
        await asyncio.sleep(0)
        return t

    t = asyncio.run(scenario())
    assert len(t.notice_prompts) == 1
    assert "- exception: send step — waits over 0s on the step before it" in t.notice_prompts[0]


def test_repeats_within_a_turn_are_counted(tmp_path):
    async def scenario():
        c, _bot, t = _claude_window(tmp_path, refuse={"rich"})
        for _ in range(3):
            await _send_md(c, t)
        return t._build_prompt([])

    prompt = asyncio.run(scenario())
    assert prompt.count("- fallback: message") == 1
    assert prompt.splitlines()[1].endswith("×3")


def test_a_failure_on_a_reply_to_a_notice_prompt_rides_the_next_user_prompt(tmp_path):
    async def scenario():
        c, _bot, t = _claude_window(tmp_path, refuse={"rich", "plain"})
        submitted: list[str] = []

        async def submit(prompt, reply_to=None):
            submitted.append(prompt)

        t.submit = submit
        await _send_md(c, t, "first")
        after_first = list(t.notice_prompts)
        await _send_md(c, t, "a reply to the notice prompt")  # fails too
        after_reply = list(t.notice_prompts)
        await c.send("next question", t.window)  # the user's next prompt
        await asyncio.sleep(drv.MESSAGE_DEBOUNCE + 0.1)
        await c.settle()
        return after_first, after_reply, submitted, t

    after_first, after_reply, submitted, t = asyncio.run(asyncio.wait_for(scenario(), 15))
    assert len(after_first) == 1
    assert after_reply == after_first  # no second notice prompt: the loop cannot start
    assert len(submitted) == 1
    assert submitted[0].startswith("next question")
    assert "- not delivered: message (Rich → none)" in submitted[0]
    assert t.notice_prompt_open is False  # the user's prompt reopens immediate notices


def test_the_notice_prompt_is_submitted_as_its_own_prompt(tmp_path):
    async def scenario():
        c, _bot, t = _claude_window(tmp_path)
        t._submit_notices = ClaudeTopic._submit_notices.__get__(t)
        submitted: list[str] = []

        async def submit(prompt, reply_to=None):
            submitted.append(prompt)

        t.submit = submit
        t.on_notice(Notice(NOTICE_EXCEPTION, t.window, "send step", error="RuntimeError: x"))
        t.on_notice(Notice(NOTICE_EXCEPTION, t.window, "send step", error="RuntimeError: x"))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        return submitted, t

    submitted, t = asyncio.run(scenario())
    assert submitted == ["[Bot notice(s):\n- exception: send step — RuntimeError: x ×2\n]"]
    assert t.notice_prompt_open is True


def test_the_session_brief_says_what_each_notice_kind_asks():
    assert "[Bot notice(s)]" in SESSION_BRIEF
    assert "`not delivered`" in SESSION_BRIEF and "send it again" in SESSION_BRIEF
    assert "`exception`" in SESSION_BRIEF and "tell the user what failed" in SESSION_BRIEF
    assert "`fallback`" in SESSION_BRIEF and "no action" in SESSION_BRIEF


def test_a_shell_window_shows_the_notice_as_one_message(tmp_path):
    async def scenario(refuse):
        c = TestClient(Shell(), home=str(tmp_path))
        bot = _refusing(c, refuse)
        shell = c.core._instantiate(ShellTopic, "777", "sh")
        c.core.owners["777"] = "shell"
        await shell.send_md("**bold** output")
        for _ in range(10):
            await asyncio.sleep(0)
        return bot, [text for _mid, _thread, text in bot.created]

    bot, shown = asyncio.run(scenario({"rich"}))
    notices = [text for text in shown if text.startswith("⚠️ bot notice — ")]
    assert len(notices) == 1
    assert notices[0].startswith("⚠️ bot notice — fallback: message (Rich → plain)")
    # every form refused: the notice message fails too, and that failure is only logged
    bot, shown = asyncio.run(scenario({"rich", "plain"}))
    assert shown == []
    attempts = len(bot.tried)
    assert attempts <= 8, bot.tried  # one send's forms and one notice's forms, no loop


# ── Review fixes: a shown notice, a stop, the untested paths ──────────


def test_a_shell_window_with_a_refused_bottom_panel_shows_one_notice(tmp_path):
    """A notice shown as a message moves no bottom frame: the panel's fallback notice is
    one message, not a stream of notices each re-sending the panel below it."""

    async def scenario():
        c = TestClient(Shell(), home=str(tmp_path))
        bot = _refusing(c, {"rich"})
        shell = c.core._instantiate(ShellTopic, "777", "sh")
        c.core.owners["777"] = "shell"
        shell.show_bottom_frame("panel", "panel plain", "**panel** md")
        await asyncio.sleep(1.0)
        return bot

    bot = asyncio.run(scenario())
    texts = [text for _mid, _thread, text in bot.created]
    assert sum(text.startswith("⚠️ bot notice") for text in texts) == 1
    assert len(texts) == 2, texts  # the panel and its one notice


def test_a_repeated_notice_in_a_window_with_no_instance_counts_up_on_one_message(tmp_path):
    async def scenario():
        c = TestClient(home=str(tmp_path))
        for _ in range(3):
            c.core.notice(Notice(NOTICE_EXCEPTION, "888", "button handler", error="E: x"))
            await asyncio.sleep(0.05)
        shown_before = [text for _mid, thread, text in c.bot.created if thread == 888]
        await c.send("hello", "888")  # the window's next message: a notice shows afresh
        c.core.notice(Notice(NOTICE_EXCEPTION, "888", "button handler", error="E: x"))
        await asyncio.sleep(0.05)
        shown_after = [text for _mid, thread, text in c.bot.created if thread == 888]
        return c, shown_before, shown_after

    c, shown_before, shown_after = asyncio.run(scenario())
    assert shown_before == ["⚠️ bot notice — exception: button handler — E: x"]
    notice_id = next(mid for mid, thread, _text in c.bot.created if thread == 888)
    assert dict(c.bot.edits)[notice_id] == "⚠️ bot notice — exception: button handler — E: x ×3"
    assert shown_after[-1] == "⚠️ bot notice — exception: button handler — E: x"
    assert len([t for t in shown_after if t.startswith("⚠️")]) == 2


def test_a_notice_while_the_bot_stops_is_held_and_delivered_after_the_restart(tmp_path):
    from claude_fakes import cancel_task

    async def scenario():
        c, _bot, t = _claude_window(tmp_path)
        t._submit_notices = ClaudeTopic._submit_notices.__get__(t)
        submitted: list[str] = []

        async def submit(prompt, reply_to=None):
            submitted.append(prompt)

        t.submit = submit
        t.on_notice(Notice(NOTICE_FALLBACK, t.window, "message", "Rich → plain", "E: f"))
        c.core.shutting_down = True
        c.core.notice(Notice(NOTICE_EXCEPTION, t.window, "shutdown", error="RuntimeError: x"))
        await t.on_shutdown()
        for _ in range(3):
            await asyncio.sleep(0)
        during_stop = list(submitted)
        # the restart: a new process revives the window from its store
        c.core.shutting_down = False
        revived = window(c, tmp_path)
        await revived.on_revive()
        await asyncio.sleep(0)
        await cancel_task(revived.mirror_task)
        return during_stop, revived

    during_stop, revived = asyncio.run(scenario())
    assert during_stop == []  # no Claude prompt while the bot stops
    assert revived.notice_prompts == [
        "[Bot notice(s):\n- fallback: message (Rich → plain) — E: f\n"
        "- exception: shutdown — RuntimeError: x\n]"
    ]
    assert revived.saved.get("notices") is None  # delivered once


def test_a_live_message_fallback_raises_a_notice():
    class _RenderedRefused(MockBot):
        async def edit_message_text(self, text=None, **kw):
            rich = kw.get("rich_message")
            if rich is not None and "<b>" in rich.html:
                raise _Refused()
            return await super().edit_message_text(text, **kw)

    async def scenario():
        tx = telegram_transport(_RenderedRefused())
        notices: list[Notice] = []
        tx.on_notice = notices.append
        assert await asyncio.wait_for(tx.land_frame("7", 50, "x", "**x**"), 5)
        return notices

    notices = asyncio.run(scenario())
    assert [(n.kind, n.what, n.forms) for n in notices] == [
        (NOTICE_FALLBACK, "live message", "Rich → escaped Rich")
    ]


def test_a_flooded_later_form_of_a_live_message_is_no_fallback_yet():
    from aiogram.exceptions import TelegramRetryAfter

    class _FloodOnce(MockBot):
        flooded = False

        async def edit_message_text(self, text=None, **kw):
            rich = kw.get("rich_message")
            if rich is not None and "<b>" in rich.html:
                raise _Refused()
            if not self.flooded:
                self.flooded = True
                raise TelegramRetryAfter(method=None, message="Too Many Requests", retry_after=1)
            return await super().edit_message_text(text, **kw)

    async def scenario():
        tx = telegram_transport(_FloodOnce())
        notices: list[Notice] = []
        tx.on_notice = notices.append
        assert await asyncio.wait_for(tx.land_frame("7", 50, "x", "**x**"), 10)
        return notices

    notices = asyncio.run(scenario())
    assert [(n.kind, n.forms) for n in notices] == [(NOTICE_FALLBACK, "Rich → escaped Rich")]


def test_an_edit_cut_to_fit_raises_a_not_delivered_notice():
    async def scenario():
        tx = telegram_transport(MockBot())
        notices: list[Notice] = []
        tx.on_notice = notices.append
        await tx.edit("7", 50, "x" * 5000)
        return notices

    notices = asyncio.run(scenario())
    assert [(n.kind, n.what, n.forms, n.size) for n in notices] == [
        (NOTICE_NOT_DELIVERED, "edit", "cut to fit", 5000)
    ]


def test_a_form_given_up_after_network_retries_names_its_error(monkeypatch):
    from aiogram.exceptions import TelegramNetworkError

    from tgforge.base import kernel

    monkeypatch.setattr(kernel, "TRANSIENT_BACKOFF", 0.001)

    class _Offline(MockBot):
        async def send_rich_message(self, chat_id, rich_message, **kw):
            if "<b>" in rich_message.html:
                raise TelegramNetworkError(method=None, message="connection reset")
            return await super().send_rich_message(chat_id, rich_message, **kw)

    async def scenario():
        tx = telegram_transport(_Offline())
        notices: list[Notice] = []
        tx.on_notice = notices.append
        await tx.send_rich("7", "**x**")
        return notices

    notices = asyncio.run(scenario())
    assert [(n.forms, n.error) for n in notices] == [
        ("Rich → escaped Rich", "TelegramNetworkError: HTTP Client says - connection reset")
    ]


def test_plain_text_refused_as_escaped_rich_names_that_form():
    async def scenario():
        bot = _RefusingBot({"rich"})
        tx = telegram_transport(bot)
        notices: list[Notice] = []
        tx.on_notice = notices.append
        await tx.send("7", "plain text")
        return notices

    notices = asyncio.run(scenario())
    assert [(n.kind, n.forms) for n in notices] == [(NOTICE_FALLBACK, "escaped Rich → plain")]


def test_an_attachment_that_fails_reaches_claude_as_a_not_delivered_notice(tmp_path):
    async def scenario():
        c, _bot, t = _claude_window(tmp_path)
        t._open_holder()
        await drain(c, t)
        await t._finalize_turn("done\n[[attach]] /nonexistent/report.pdf")
        await drain(c, t)
        for _ in range(3):
            await asyncio.sleep(0)
        return t

    t = asyncio.run(scenario())
    assert len(t.notice_prompts) == 1
    assert (
        "- not delivered: attachment /nonexistent/report.pdf — RuntimeError: no such file"
        in (t.notice_prompts[0])
    )


def test_a_failed_helper_install_raises_a_notice_in_general(tmp_path, monkeypatch):
    from pathlib import Path

    home = tmp_path / "home"
    home.mkdir()
    (home / ".local").write_text("a file where the bin dir's parent should be")
    (home / ".bashrc").write_text("# rc\n")
    (home / ".bashrc").chmod(0o444)
    monkeypatch.setattr(Path, "home", lambda: home)

    async def scenario():
        c = client(tmp_path)
        await c.core.plugin_by_id["claude"].on_startup()
        for _ in range(10):
            await asyncio.sleep(0.01)
        return [text for _mid, thread, text in c.bot.created if thread is None]

    shown = asyncio.run(scenario())
    assert any(t.startswith("⚠️ bot notice — exception: claude-as install — ") for t in shown)
    assert any(
        t.startswith("⚠️ bot notice — exception: shell wrapper install (.bashrc) — Permission")
        for t in shown
    ), shown


def test_a_held_fallback_outlives_the_restart_with_no_notice_during_the_stop(tmp_path):
    from claude_fakes import cancel_task

    async def scenario():
        c, _bot, t = _claude_window(tmp_path)
        t.on_notice(Notice(NOTICE_FALLBACK, t.window, "message", "Rich → plain", "E: f"))
        c.core.shutting_down = True
        await t.on_shutdown()
        c.core.shutting_down = False
        revived = window(c, tmp_path)
        await revived.on_revive()
        await cancel_task(revived.mirror_task)
        return revived._build_prompt([])

    prompt = asyncio.run(scenario())
    assert prompt == "[Bot notice(s):\n- fallback: message (Rich → plain) — E: f\n]"
