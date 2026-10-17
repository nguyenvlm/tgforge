"""The finalized-turn layout (AGENTS.md → Finalized-turn format): the live card becomes
tool-call message 1 (the summary line over the folded timeline), the rest of the
timeline follows in more collapsed messages, then the answer in one or more messages
(suggestion buttons on the last), then attachments — nothing cut, every piece sent in
order and retried until it lands. The settle runs as a send step, so the reader keeps
reading CLI events meanwhile, and the next turn's card opens only after it."""

from __future__ import annotations

import asyncio

from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from claude_fakes import (
    FakeProc,
    assistant_text,
    cancel_task,
    client,
    drain,
    replay,
    result,
    screen,
    texts,
    tool_use,
    window,
)

from tgforge.base.ui import MAX_MSG
from tgforge.plugins.claude import background
from tgforge.plugins.claude.render import tool_line


class _Flood(TelegramRetryAfter):
    def __init__(self, retry_after):
        self.retry_after = retry_after


class _Gone(TelegramBadRequest):
    def __init__(self):
        self.message = "Bad Request: message to edit not found"

    def __str__(self):
        return self.message


async def _turn(c, t, timeline=(), answer="the answer"):
    """Open a turn, fill its timeline, and settle it."""
    t._open_holder()
    await drain(c, t)
    for kind, payload in timeline:
        t.turn.timeline.append((kind, payload))
    await t._finalize_turn(answer)
    await drain(c, t)


def _is_fold(text: str) -> bool:
    return "**>" in text and text.endswith("||")


def test_a_long_timeline_folds_into_several_messages_with_every_entry(tmp_path):
    async def scenario():
        c = client(tmp_path)
        t = window(c, tmp_path)
        entries = [("tool", f"💻 Bash: step {i:03d} " + "x" * 300) for i in range(40)]
        await _turn(c, t, entries)

        shown = texts(c)
        folds = [text for text in shown if _is_fold(text)]
        assert len(folds) >= 3  # 40 × ~320 chars cannot fit one message
        assert all(len(text) <= MAX_MSG for text in folds)
        joined = "".join(folds)
        for i in range(40):
            assert f"step {i:03d}" in joined  # every entry present
        assert "…" not in joined.replace("↳", "")  # nothing cut
        assert shown[-1] == "the answer"  # the answer is the bottom-most message
        summary, quote = folds[0].split("\n", 1)
        assert summary.startswith("🔧 40")  # message 1 opens with the summary line, plain
        assert quote.startswith("**>💻 Bash: step 000")  # the block holds only the timeline
        assert all(f.startswith("**>↳ ") for f in folds[1:])  # later pages: `↳ i/n` in the block

    asyncio.run(scenario())


def test_a_long_tool_detail_is_not_cut():
    command = "python run.py " + "--flag value " * 20
    assert tool_line("Bash", {"command": command}).endswith(" ".join(command.split()))


def test_an_entry_longer_than_a_message_is_split_across_messages(tmp_path):
    async def scenario():
        c = client(tmp_path)
        t = window(c, tmp_path)
        await _turn(c, t, [("thinking", "word " * 2000), ("tool", "🔧 last")])
        folds = [text for text in texts(c) if _is_fold(text)]
        assert len(folds) >= 2
        assert sum(text.count("word") for text in folds) == 2000

    asyncio.run(scenario())


def test_no_tools_gives_a_summary_only_message_and_the_answer(tmp_path):
    async def scenario():
        c = client(tmp_path)
        t = window(c, tmp_path)
        await _turn(c, t, answer="just text")
        shown = texts(c)
        assert len(shown) == 2
        assert "tokens" in shown[0] and "🔧" not in shown[0]
        assert "**>" not in shown[0] and not shown[0].endswith("||")  # no block
        assert shown[1] == "just text"

    asyncio.run(scenario())


def test_an_empty_result_answers_no_reply(tmp_path):
    async def scenario():
        c = client(tmp_path)
        t = window(c, tmp_path)
        await _turn(c, t, [("tool", "🔧 did a thing")], answer="")
        assert texts(c)[-1].replace("\\", "") == "(no reply)"

    asyncio.run(scenario())


def test_the_live_card_becomes_message_1(tmp_path):
    async def scenario():
        c = client(tmp_path)
        t = window(c, tmp_path)
        t._open_holder()
        await drain(c, t)
        card = t.turn.card_id
        t.turn.timeline.append(("tool", "🔧 one"))
        await t._finalize_turn("done")
        await drain(c, t)
        assert screen(c)[0][0] == card and _is_fold(screen(c)[0][1])

    asyncio.run(scenario())


def test_a_failed_card_edit_deletes_the_card_and_sends_message_1_fresh(tmp_path):
    async def scenario():
        c = client(tmp_path)
        t = window(c, tmp_path)
        t._open_holder()
        await drain(c, t)
        card = t.turn.card_id
        t.turn.timeline.append(("tool", "🔧 one"))

        async def gone(text, **kw):
            raise _Gone()

        c.bot.edit_message_text = gone
        await t._finalize_turn("done")
        await drain(c, t)
        assert card in c.bot.deleted  # the live frame never stays as the finalized card
        shown = texts(c)
        assert _is_fold(shown[0]) and "🔧 one" in shown[0].replace("\\", "")
        assert shown[1] == "done"

    asyncio.run(scenario())


def test_a_ten_chunk_answer_sends_all_ten(tmp_path):
    async def scenario():
        c = client(tmp_path)
        t = window(c, tmp_path)
        answer = "\n".join(f"chunk {i} " + "y" * 1500 for i in range(10))
        await _turn(c, t, answer=answer)
        shown = texts(c)
        assert "(output truncated)" not in " ".join(shown)
        assert sum(text.count("chunk ") for text in shown) == 10

    asyncio.run(scenario())


def test_a_flood_waited_piece_is_retried_and_order_holds(tmp_path):
    async def scenario():
        c = client(tmp_path)
        t = window(c, tmp_path)
        real_send = c.bot.send_message
        floods = {"n": 0}

        async def send(chat_id, text, **kw):
            if "PIECE 2" in text and floods["n"] < 2:
                floods["n"] += 1
                raise _Flood(0.05)
            return await real_send(chat_id, text, **kw)

        c.bot.send_message = send
        answer = "\n".join(f"PIECE {i} " + "z" * 3000 for i in range(4))
        await _turn(c, t, answer=answer)
        pieces = [text for text in texts(c) if "PIECE" in text]
        order = [int(text.split("PIECE ")[1].split()[0]) for text in pieces]
        assert order == [0, 1, 2, 3]  # the flooded piece landed, in place, no gap
        assert floods["n"] == 2

    asyncio.run(scenario())


def test_suggestion_buttons_ride_the_last_answer_message(tmp_path):
    async def scenario():
        c = client(tmp_path)
        t = window(c, tmp_path)
        markups = {}
        real_send = c.bot.send_message

        async def send(chat_id, text, **kw):
            msg = await real_send(chat_id, text, **kw)
            markups[msg.message_id] = kw.get("reply_markup")
            return msg

        c.bot.send_message = send
        answer = "\n".join(f"part {i} " + "w" * 3000 for i in range(3))
        await _turn(c, t, answer=answer + "\n[[suggest]] yes | no")
        answer_ids = [mid for mid, text in screen(c) if "part " in text]
        assert markups[answer_ids[-1]] is not None
        assert all(markups[mid] is None for mid in answer_ids[:-1])

    asyncio.run(scenario())


def test_order_is_folds_then_answer_then_attachments_then_panel(tmp_path):
    async def scenario():
        c = client(tmp_path)
        t = window(c, tmp_path)
        out = tmp_path / "job.output"
        out.write_text("working\n")
        attachment = tmp_path / "report.txt"
        attachment.write_text("data")
        t.background_tasks = {
            "b1": {"path": str(out), "label": "a job", "start": 0.0, "done": None}
        }
        t._ensure_background_updater()
        await drain(c, t)
        entries = [("tool", f"🔧 step {i} " + "v" * 400) for i in range(15)]
        await _turn(c, t, entries, answer=f"the answer\n[[attach]] {attachment}")
        await drain(c, t)

        shown = texts(c)
        kinds = [
            "fold"
            if _is_fold(text)
            else "panel"
            if "background ·" in text
            else "file"
            if text == "<document>"
            else "answer"
            for text in shown
        ]
        assert kinds[-3:] == ["answer", "file", "panel"]
        assert kinds[: kinds.index("answer")] == ["fold"] * kinds.index("answer")
        assert kinds.count("panel") == 1
        await cancel_task(t.background_updater_task)

    asyncio.run(scenario())


def test_the_reader_keeps_consuming_events_while_the_settle_sends(tmp_path):
    """The settle is a send step, not reader work: with every send blocked, the reader
    still drains the CLI's events after the result."""

    async def scenario():
        c = client(tmp_path)
        t = window(c, tmp_path)
        release = asyncio.Event()
        real_send = c.bot.send_message

        async def slow_send(chat_id, text, **kw):
            if "SETTLE" in text:
                await release.wait()
            return await real_send(chat_id, text, **kw)

        c.bot.send_message = slow_send
        t._open_holder()
        await drain(c, t)
        hold = asyncio.Event()
        t.proc = FakeProc(
            [result("SETTLE answer"), *[assistant_text(f"e{i}") for i in range(5)], hold]
        )
        t.pending_writes = [
            {"is_initiator": False, "bubble_id": None, "reply_to": None, "turn_seq": 1}
        ]
        reader = asyncio.create_task(t._reader())
        await asyncio.sleep(0.1)
        assert t.proc.stdout.consumed == 6  # every event read while the answer is stuck
        release.set()
        hold.set()
        await asyncio.wait_for(reader, 5)
        await drain(c, t)

    asyncio.run(scenario())


def test_a_message_queued_during_the_settle_gets_a_fresh_card_below_the_answer(tmp_path):
    """Bug 3: a message ack'd right after the boundary (while the previous turn settles)
    becomes the next turn. Its "queued ✓" bubble is deleted and the new card is sent
    below the whole previous answer — never edited in place above it, never between
    answer pieces."""

    async def scenario():
        c = client(tmp_path)
        t = window(c, tmp_path)
        release = asyncio.Event()
        real_send = c.bot.send_message

        async def slow_send(chat_id, text, **kw):
            if "ANSWER 1" in text:
                await release.wait()
            return await real_send(chat_id, text, **kw)

        c.bot.send_message = slow_send
        t._open_holder()
        t.pending_writes = [{"is_initiator": True, "bubble_id": None, "reply_to": None}]
        await drain(c, t)
        user_msg = c.bot._id()
        boundary = t._finalize_turn

        async def boundary_then_message(*args, **kwargs):
            await boundary(*args, **kwargs)
            c.bot.created.append((user_msg, 555, "next prompt"))
            await t.submit("next prompt", reply_to=user_msg)  # mid-settle, busy still set

        t._finalize_turn = boundary_then_message
        echo = asyncio.Event()
        answer = "\n".join(f"ANSWER {i} " + "q" * 3000 for i in range(3))
        t.proc = FakeProc([result(answer), echo, replay("next prompt"), asyncio.Event()])
        reader = asyncio.create_task(t._reader())
        await asyncio.sleep(0.05)
        release.set()
        echo.set()  # the CLI starts the queued turn
        await asyncio.sleep(0.05)
        await drain(c, t)

        answers = [mid for mid, text in screen(c) if text.startswith("ANSWER")]
        acks = [mid for mid, _tid, text in c.bot.created if text == "queued ✓"]
        assert len(answers) == 3 and acks
        assert all(ack in c.bot.deleted for ack in acks)
        assert t.turn_open and t.turn.card_id > max(answers)  # below every answer piece
        assert t.turn.reply_to == user_msg  # the card replies to the user's message
        reader.cancel()
        await asyncio.gather(reader, return_exceptions=True)

    asyncio.run(scenario())


def test_the_next_turn_card_waits_for_the_previous_settle(tmp_path):
    async def scenario():
        c = client(tmp_path)
        t = window(c, tmp_path)
        release = asyncio.Event()
        real_send = c.bot.send_message

        async def slow_send(chat_id, text, **kw):
            if text == "LAST":
                await release.wait()
            return await real_send(chat_id, text, **kw)

        c.bot.send_message = slow_send
        t._open_holder()
        await drain(c, t)
        await t._finalize_turn("LAST")
        t._open_holder()  # the next turn opens while the settle is still sending
        await asyncio.sleep(0.05)
        assert t.turn.card_id is None  # its card is queued behind the settle
        release.set()
        await drain(c, t)
        assert t.turn.card_id > next(mid for mid, text in screen(c) if text == "LAST")

    asyncio.run(scenario())


def test_the_heartbeat_never_paints_a_closed_turns_card(tmp_path, monkeypatch):
    async def scenario():
        from tgforge.plugins.claude import driver as drv

        monkeypatch.setattr(drv, "EDIT_INTERVAL", 0.01)
        c = client(tmp_path)
        t = window(c, tmp_path)
        t._open_holder()
        await drain(c, t)
        card = t.turn.card_id
        t.turn.timeline.append(("tool", "🔧 one"))
        await t._finalize_turn("done")
        await drain(c, t)
        await asyncio.sleep(0.05)
        await drain(c, t)
        assert _is_fold(dict(c.bot.edits)[card])  # still message 1, not a live frame
        await t._stop_heartbeat()

    asyncio.run(scenario())


def test_tool_use_events_become_timeline_entries(tmp_path):
    async def scenario():
        c = client(tmp_path)
        t = window(c, tmp_path)
        t._open_holder()
        await drain(c, t)
        t.proc = FakeProc([tool_use("Bash", {"command": "ls"}), result("ok")])
        t.pending_writes = []
        await asyncio.wait_for(t._reader(), 5)
        await drain(c, t)
        assert "💻 Bash: ls" in texts(c)[0].replace("\\", "")
        assert background.visible_tasks(t, 0) == []

    asyncio.run(scenario())


def test_a_late_settle_keeps_the_next_turns_launch_labels(tmp_path):
    async def scenario():
        c = client(tmp_path)
        t = window(c, tmp_path)
        release = asyncio.Event()
        real_send = c.bot.send_message

        async def slow_send(chat_id, text, **kw):
            if text == "LAST":
                await release.wait()
            return await real_send(chat_id, text, **kw)

        c.bot.send_message = slow_send
        t._open_holder()
        await drain(c, t)
        t.background_labels = {"old": "the closed turn's job"}
        await t._finalize_turn("LAST")
        t.background_labels["t9"] = "the next turn's job"  # read while the settle sends
        release.set()
        await drain(c, t)
        assert t.background_labels == {"t9": "the next turn's job"}

    asyncio.run(scenario())


def test_a_hundred_page_timeline_keeps_every_page_within_a_message():
    """Entries sized to fill a page exactly under a 2-digit `↳ 99/99` header: past 99
    pages the 3-digit header must still fit, so packing sizes it on the real count."""
    from tgforge.base.ui import expandable, expandable_pages

    size = next(n for n in range(MAX_MSG, 0, -1) if len(expandable("↳ 99/99", "x" * n)) <= MAX_MSG)
    pages = expandable_pages("🔧 120", ["x" * size] * 120)
    assert len(pages) >= 100
    assert all(len(md) <= MAX_MSG for md, _plain in pages)  # 3-digit headers fit too
    assert sum(md.count("x") for md, _plain in pages) == 120 * size  # nothing lost
    assert pages[-1][1].startswith(f"↳ {len(pages)}/{len(pages)}")
    assert pages[0][0].startswith("🔧 120\n**>x")  # page 1: plain summary over its block
