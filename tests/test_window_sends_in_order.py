"""Every new message or delete of a Claude window is a send step, commands' replies
included, so nothing lands between a settling turn's answer pieces (AGENTS.md →
Message-ordering invariant)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from claude_fakes import THREAD, FakeProc, client, drain, screen, window


async def _settling(c, t):
    """A turn whose settle is stuck after its first answer piece; returns the release."""
    release = asyncio.Event()
    real_send = c.bot.send_message

    async def slow_send(chat_id, text, **kw):
        if "ANSWER 1" in text:
            await release.wait()
        return await real_send(chat_id, text, **kw)

    c.bot.send_message = slow_send
    t.proc = FakeProc()
    t._open_holder()
    await drain(c, t)
    await t._finalize_turn("\n".join(f"ANSWER {i} " + "q" * 3000 for i in range(3)))
    await asyncio.sleep(0.05)  # piece 0 out, piece 1 stuck
    return release


def _below_every_answer(c, text):
    """Whether the message first sent as `text` sits below all three answer pieces."""
    answers = [mid for mid, body in screen(c) if body.startswith("ANSWER")]
    target = next(mid for mid, tid, body in c.bot.created if tid == THREAD and body == text)
    return len(answers) == 3 and target > max(answers)


def test_the_cancel_bubble_never_lands_between_answer_pieces(tmp_path):
    async def scenario():
        c = client(tmp_path)
        t = window(c, tmp_path)
        release = await _settling(c, t)
        cancel = asyncio.create_task(t.cancel(None))  # busy is still set: "cancelling..."
        await asyncio.sleep(0.05)
        release.set()
        await asyncio.wait_for(cancel, 5)
        await drain(c, t)
        assert _below_every_answer(c, "cancelling...")

    asyncio.run(scenario())


def test_a_command_reply_through_the_router_waits_for_the_settle(tmp_path):
    async def scenario():
        c = client(tmp_path)
        t = window(c, tmp_path)
        release = await _settling(c, t)
        await c.send("/status", thread_id=THREAD)  # an inline command: the kernel replies
        await asyncio.sleep(0.05)
        release.set()
        await asyncio.wait_for(c.settle(), 5)
        await drain(c, t)
        status = next(body for _mid, body in screen(c) if body.startswith("work · "))
        assert _below_every_answer(c, status)

    asyncio.run(scenario())


def test_a_kill_reply_waits_for_the_settle(tmp_path):
    async def scenario():
        c = client(tmp_path)
        t = window(c, tmp_path)
        release = await _settling(c, t)
        kill = asyncio.create_task(t.kill_job(None))  # no jobs: a plain reply
        await asyncio.sleep(0.05)
        release.set()
        await asyncio.wait_for(kill, 5)
        await drain(c, t)
        assert _below_every_answer(c, "no running background jobs")

    asyncio.run(scenario())


def test_a_queued_send_hands_its_error_to_the_caller(tmp_path):
    async def scenario():
        c = client(tmp_path)
        t = window(c, tmp_path)
        with pytest.raises(RuntimeError, match="no such file"):
            await t.send_file(Path(tmp_path / "absent.bin"))

    asyncio.run(scenario())


def test_closing_the_window_releases_a_waiting_send(tmp_path):
    async def scenario():
        c = client(tmp_path)
        t = window(c, tmp_path)
        await _settling(c, t)
        waiting = asyncio.create_task(t.send("behind the settle"))
        await asyncio.sleep(0.05)
        t.proc = None  # the fake CLI has no OS process to reap
        await t.on_close()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(waiting, 2)  # released, not hung

    asyncio.run(scenario())


def test_a_kill_menu_opened_during_a_settle_waits_for_it(tmp_path):
    async def scenario():
        c = client(tmp_path)
        t = window(c, tmp_path)
        t.background_tasks = {
            "b1": {
                "path": str(tmp_path / "b1.output"),
                "label": "a job",
                "start": 0.0,
                "done": None,
            }
        }
        release = await _settling(c, t)
        menu = asyncio.create_task(t.kill_job(None))  # no name: the job picker menu
        await asyncio.sleep(0.05)
        release.set()
        await asyncio.sleep(0.05)
        await drain(c, t)
        assert _below_every_answer(c, "🛑 Stop which background job?")
        menu.cancel()
        await asyncio.gather(menu, return_exceptions=True)

    asyncio.run(scenario())


def test_the_help_reply_in_a_claude_window_waits_for_the_settle(tmp_path):
    async def scenario():
        c = client(tmp_path)
        t = window(c, tmp_path)
        release = await _settling(c, t)
        task = asyncio.create_task(c.core._unhandled(c.core.chat_id, THREAD))
        await asyncio.sleep(0.05)
        release.set()
        await asyncio.wait_for(task, 5)
        await drain(c, t)
        help_text = c.core.help_text(THREAD)
        assert _below_every_answer(c, help_text)

    asyncio.run(scenario())


@pytest.mark.parametrize("prompt", ["menu", "ask_buttons", "ask_text"])
def test_a_prompt_cancelled_while_its_send_is_queued_leaves_no_state(tmp_path, prompt):
    """The window closes while the prompt's send waits behind a settle: no tap future
    or active prompt is left registered for a keyboard that never showed."""

    async def scenario():
        c = client(tmp_path)
        t = window(c, tmp_path)
        await _settling(c, t)
        calls = {
            "menu": lambda: t.menu("pick", [("a", "a")]),
            "ask_buttons": lambda: t.ask_buttons("sure?", [("yes", "yes")]),
            "ask_text": lambda: t.ask_text("name?"),
        }
        task = asyncio.create_task(calls[prompt]())
        await asyncio.sleep(0.05)
        assert c.core.pending_callbacks  # registered early, so an instant tap is caught
        assert THREAD not in c.core._active_prompt  # not active before it is on screen
        t.proc = None  # the fake CLI has no OS process to reap
        await t.on_close()
        await asyncio.gather(task, return_exceptions=True)
        assert c.core.pending_callbacks == {}
        assert THREAD not in c.core._active_prompt

    asyncio.run(scenario())
