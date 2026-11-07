"""The reply note a Claude prompt carries: the full replied text, or only the selected quote."""

from __future__ import annotations

from types import SimpleNamespace

from tgforge.plugins.claude import ClaudeTopic


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
    return SimpleNamespace(
        text="what about this?",
        forward_origin=None,
        reply_to_message=reply,
        quote=quote,
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
    reply = _reply(caption="a caption with the selected words", photo=[object()])
    note = _prefix(_msg(reply, quote=_quote("selected words")))
    assert note == "[Quoting Alice: selected words [with attachment]]"


def test_reply_to_media_only_message_is_attachment():
    assert _prefix(_msg(_reply(document=object()))) == "[Replying to Alice: (attachment)]"


def test_reply_to_topic_opening_message_adds_no_note():
    reply = _reply(topic_created=SimpleNamespace(name="topic"))
    assert _prefix(_msg(reply)) == ""
