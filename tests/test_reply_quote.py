"""The reply note a Claude prompt carries: the full replied text, or only the selected quote;
a bot's Rich Message read as plain text when replied to or forwarded."""

from __future__ import annotations

from types import SimpleNamespace

from aiogram.types import Message

from tgforge.plugins.claude import ClaudeTopic
from tgforge.testing import telegram_inbound


def _reply(text=None, caption=None, photo=None, document=None, topic_created=None):
    return SimpleNamespace(
        text=text,
        caption=caption,
        photo=photo,
        document=document,
        forum_topic_created=topic_created,
        from_user=SimpleNamespace(full_name="Alice"),
    )


def _msg(reply, quote=None):
    return telegram_inbound(
        "what about this?", forward_origin=None, reply_to_message=reply, quote=quote
    )


def _quote(text):
    return SimpleNamespace(text=text, position=0, entities=None, is_manual=True)


def _prefix(message) -> str:
    return ClaudeTopic._context_prefix(None, message)


def test_reply_to_long_message_carries_full_text():
    long_text = "".join(f"line {i}. " for i in range(200))
    assert len(long_text) > 500
    assert _prefix(_msg(_reply(text=long_text))) == f"[Replying to Alice: {long_text.strip()}]"


def test_reply_with_selected_part_carries_only_the_quote():
    text = "first part. " * 60 + "the selected bullet"
    note = _prefix(_msg(_reply(text=text), quote=_quote("the selected bullet")))
    assert note == "[Quoting Alice: the selected bullet]"


def test_quote_of_captioned_attachment_keeps_attachment_tail():
    reply = _reply(
        caption="a caption with the selected words", photo=[SimpleNamespace(file_id="p")]
    )
    note = _prefix(_msg(reply, quote=_quote("selected words")))
    assert note == "[Quoting Alice: selected words [with attachment]]"


def test_reply_to_media_only_message_is_attachment():
    assert (
        _prefix(_msg(_reply(document=SimpleNamespace(file_id="d", file_name=None))))
        == "[Replying to Alice: (attachment)]"
    )


def test_reply_to_topic_opening_message_adds_no_note():
    reply = _reply(topic_created=SimpleNamespace(name="topic"))
    assert _prefix(_msg(reply)) == ""


def test_quote_of_a_message_with_no_readable_text_still_carries_the_quote():
    note = _prefix(_msg(_reply(), quote=_quote("the selected words")))
    assert note == "[Quoting Alice: the selected words]"


# A bot answer as Telegram returns it: a Rich Message, with no `text` or `caption`
_BOT_ANSWER = {
    "message_id": 10,
    "date": 1700000000,
    "chat": {"id": 100, "type": "supergroup", "title": "Group", "is_forum": True},
    "from": {"id": 42, "is_bot": True, "first_name": "mybot"},
    "rich_message": {
        "blocks": [
            {"type": "paragraph", "text": ["Delete the ", {"type": "bold", "text": "branch"}]},
            {
                "type": "list",
                "items": [
                    {"label": "1.", "blocks": [{"type": "paragraph", "text": "push it"}]},
                    {"label": "2.", "blocks": [{"type": "paragraph", "text": "bundle it"}]},
                ],
            },
            {
                "type": "table",
                "cells": [
                    [
                        {"align": "left", "valign": "top", "text": "repo"},
                        {"align": "left", "valign": "top", "text": "tip"},
                    ]
                ],
            },
            {
                "type": "expandable_blockquote",
                "text": ["see ", {"type": "url", "text": "notes.md", "url": "notes.md"}],
            },
        ]
    },
}


def _bot_answer() -> Message:
    return Message.model_validate(_BOT_ANSWER)


def test_reply_to_a_rich_message_carries_its_plain_text():
    note = _prefix(_msg(_bot_answer()))
    assert note == (
        "[Replying to mybot: Delete the branch\n1. push it\n2. bundle it\nrepo | tip\nsee notes.md]"
    )


def test_quote_of_a_rich_message_carries_the_quote():
    note = _prefix(_msg(_bot_answer(), quote=_quote("Delete the branch")))
    assert note == "[Quoting mybot: Delete the branch]"


def test_forwarded_rich_message_carries_its_plain_text():
    answer = _bot_answer()
    message = telegram_inbound(
        None, rich_message=answer.rich_message, forward_origin=None, reply_to_message=None
    )
    assert message.text == "Delete the branch\n1. push it\n2. bundle it\nrepo | tip\nsee notes.md"


def test_rich_message_credits_and_button_labels_are_read_and_their_targets_are_not():
    answer = Message.model_validate(
        dict(
            _BOT_ANSWER,
            rich_message={
                "blocks": [
                    {
                        "type": "blockquote",
                        "blocks": [{"type": "paragraph", "text": "a saying"}],
                        "credit": "its author",
                    },
                    {
                        "type": "paragraph",
                        "text": [
                            "tap ",
                            {
                                "type": "button",
                                "button": {"text": "here", "url": "https://x.example"},
                            },
                        ],
                    },
                    {
                        "type": "buttons",
                        "buttons": [
                            {"text": "Yes", "callback_data": "y"},
                            {"text": "No", "callback_data": "n"},
                        ],
                    },
                ]
            },
        )
    )
    assert _prefix(_msg(answer)) == "[Replying to mybot: a saying\nits author\ntap here\nYes\nNo]"


def test_rich_message_captions_follow_their_content_and_file_details_stay_out():
    photo = {"file_id": "photo-id", "file_unique_id": "u", "width": 1, "height": 1}
    answer = Message.model_validate(
        dict(
            _BOT_ANSWER,
            rich_message={
                "blocks": [
                    {
                        "type": "table",
                        "cells": [[{"align": "left", "valign": "top", "text": "a | b"}]],
                        "caption": "the table",
                    },
                    {
                        "type": "photo",
                        "photo": [photo],
                        "caption": {"text": "the diagram", "credit": "its source"},
                    },
                ]
            },
        )
    )
    assert _prefix(_msg(answer)) == (
        "[Replying to mybot: a | b\nthe table\nthe diagram\nits source]"
    )
