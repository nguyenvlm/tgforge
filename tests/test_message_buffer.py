"""Messages sent together (a forward and its comment, an album) reach Claude as one prompt,
sent once the window has been quiet for the debounce and every download has finished."""

from __future__ import annotations

import asyncio
import functools
from types import SimpleNamespace

import pytest
from claude_fakes import THREAD, client, window

from tgforge.plugins.claude import driver
from tgforge.testing import telegram_inbound

DEBOUNCE = 0.05


def run(test):
    @functools.wraps(test)
    def sync(*args, **kwargs):
        asyncio.run(test(*args, **kwargs))

    return sync


@pytest.fixture
def topic(tmp_path, monkeypatch):
    monkeypatch.setattr(driver, "MESSAGE_DEBOUNCE", DEBOUNCE)
    t = window(client(tmp_path), tmp_path)
    t.submitted = []

    async def submit(prompt, reply_to=None):
        t.submitted.append((prompt, reply_to))

    t.submit = submit
    t.download_delay = 0.0

    async def download_file(attachment, dest):
        await asyncio.sleep(t.download_delay)
        return dest

    t.download_file = download_file
    return t


def _photo(file_id):
    return [SimpleNamespace(file_id=file_id)]


def _msg(text, message_id, **fields):
    defaults = {"forward_origin": None, "reply_to_message": None, "quote": None}
    return telegram_inbound(text, window=THREAD, message_id=message_id, **(defaults | fields))


def _forward(message_id, file_id="f"):
    origin = SimpleNamespace(type="user", sender_user=SimpleNamespace(full_name="Bob"))
    return _msg("", message_id, forward_origin=origin, photo=_photo(file_id))


async def _send(topic, message, after=0.0):
    await asyncio.sleep(after)
    await topic._buffer_message(message, message.text)


async def _settle(topic):
    while topic.message_flush_task is not None:
        await asyncio.sleep(DEBOUNCE / 5)


@run
async def test_comment_and_forward_become_one_prompt(topic):
    await _send(topic, _msg("Store this table", 10))
    await _send(topic, _forward(11), after=0.013)
    await _settle(topic)
    assert len(topic.submitted) == 1
    prompt, reply_to = topic.submitted[0]
    assert reply_to == 10
    assert prompt.index("Store this table") < prompt.index("[Forwarded message from Bob]")
    assert "[Attached file(s), saved locally — Read to view:" in prompt
    assert prompt.count(".jpg") == 1


@run
async def test_forward_in_a_later_batch_joins_within_the_debounce(topic):
    await _send(topic, _msg("Store this table", 10))
    await _send(topic, _forward(11), after=DEBOUNCE * 0.8)
    await _settle(topic)
    assert len(topic.submitted) == 1
    assert "[Forwarded message from Bob]" in topic.submitted[0][0]


@run
async def test_prompt_waits_for_a_slow_download(topic):
    topic.download_delay = DEBOUNCE * 10
    await _send(topic, _msg("Store this table", 10))
    await _send(topic, _forward(11))
    await asyncio.sleep(DEBOUNCE * 3)
    assert topic.submitted == []  # the debounce passed, the download has not finished
    await _settle(topic)
    assert len(topic.submitted) == 1
    assert ".jpg" in topic.submitted[0][0]


@run
async def test_message_during_a_slow_download_joins_the_prompt(topic):
    topic.download_delay = DEBOUNCE * 10
    await _send(topic, _forward(10))
    await _send(topic, _msg("and this too", 11), after=DEBOUNCE * 3)
    await _settle(topic)
    assert len(topic.submitted) == 1
    assert "and this too" in topic.submitted[0][0]


@run
async def test_messages_further_apart_than_the_debounce_stay_separate(topic):
    await _send(topic, _msg("first", 10))
    await _send(topic, _msg("second", 11), after=DEBOUNCE * 3)
    await _settle(topic)
    assert topic.submitted == [("first", 10), ("second", 11)]


@run
async def test_lone_text_goes_out_after_the_debounce(topic):
    await _send(topic, _msg("hello", 10))
    await asyncio.sleep(DEBOUNCE / 2)
    assert topic.submitted == []
    await _settle(topic)
    assert topic.submitted == [("hello", 10)]


@run
async def test_album_becomes_one_prompt_with_every_photo(topic):
    group = {"media_group_id": "g1"}
    await _send(topic, _msg("", 10, photo=_photo("a"), caption="two shots", **group))
    await _send(topic, _msg("", 11, photo=_photo("b"), **group), after=0.01)
    await _settle(topic)
    assert len(topic.submitted) == 1
    prompt, reply_to = topic.submitted[0]
    assert reply_to == 10
    assert prompt.startswith("two shots")
    assert prompt.count(".jpg") == 2


@run
async def test_each_message_keeps_its_own_context_note(topic):
    origin_b = SimpleNamespace(type="user", sender_user=SimpleNamespace(full_name="Bob"))
    origin_c = SimpleNamespace(type="user", sender_user=SimpleNamespace(full_name="Carol"))
    await _send(topic, _msg("from bob", 10, forward_origin=origin_b))
    await _send(topic, _msg("from carol", 11, forward_origin=origin_c), after=0.01)
    await _settle(topic)
    ((prompt, _),) = topic.submitted
    assert prompt == (
        "[Forwarded message from Bob]\n\nfrom bob\n\n[Forwarded message from Carol]\n\nfrom carol"
    )
