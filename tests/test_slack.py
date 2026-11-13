"""Telegram and Slack in one kernel: the window key picks the platform, each platform keeps
its own General, owner and identity, and the real `SlackAdapter` (over a fake Web API
client) renders, classifies, threads and converts events as the design note lays out."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import aiohttp
import pytest
from slack_fakes import (
    FakeSlackClient,
    api_error,
    slack_adapter,
    slack_client,
    slack_config,
    two_platform_kernel,
)

from example.allowlist import Allowlist, AllowlistAuth
from tgforge.base import ui
from tgforge.base.app import OwnerOnly
from tgforge.base.config import BotConfig
from tgforge.base.kernel import (
    Flood,
    Inbound,
    NotFound,
    NotModified,
    Plugin,
    Refused,
    Tap,
    Topic,
    Transient,
    Transport,
    command,
    launch,
    on_unknown,
    universal,
)
from tgforge.base.slack import NAME_MAX, SlackMarkdown, channel_slug
from tgforge.testing import kernel_bot

TG_WINDOW = "1001"
SLACK_WINDOW = "slack:C9"
SLACK_GENERAL = "slack:C0"

SEEN: list[tuple] = []


@launch("/work", "open a work window")
class WorkTopic(Topic):
    id = "work"

    @command("/model", "switch the model")
    async def model(self, ctx):
        SEEN.append(("model", ctx.args))

    @on_unknown
    async def unknown(self, ctx):
        SEEN.append(("unknown", ctx.text))


class Work(Plugin):
    id = "work"
    topics = [WorkTopic]

    @universal("/usage2", "a universal the escape skips")
    async def usage2(self, ctx):
        SEEN.append(("universal", ctx.text))


def setup_function():
    SEEN.clear()


def _kernel(tmp_path, plugins=None):
    return two_platform_kernel(slack_config(str(tmp_path)), plugins or [Work()])


def _own(core, window, cls=WorkTopic):
    core.owners[window] = cls.id
    return core._instantiate(cls, window, "w")


def _slack_message(text, ts="1700000100.000100", channel="C9", user="U1", **extra):
    return {"type": "message", "channel": channel, "user": user, "text": text, "ts": ts, **extra}


# ── config ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "body, telegram, slack",
    [
        ({"token": "abc"}, True, False),
        ({"slack": {"bot_token": "xoxb", "app_token": "xapp"}}, False, True),
        ({"token": "abc", "slack": {"bot_token": "xoxb", "app_token": "xapp"}}, True, True),
    ],
    ids=["telegram-only", "slack-only", "both"],
)
def test_config_loads_each_platform_set(tmp_path, body, telegram, slack):
    path = tmp_path / "bot.json"
    path.write_text(json.dumps(body))
    cfg = BotConfig.load(path)
    assert bool(cfg.token) is telegram
    assert (cfg.slack is not None) is slack


def test_config_with_neither_platform_raises(tmp_path):
    path = tmp_path / "bot.json"
    path.write_text(json.dumps({"home": "."}))
    with pytest.raises(ValueError, match="slack"):
        BotConfig.load(path)


def test_config_save_writes_back_the_slack_identity_owner_only(tmp_path):
    path = tmp_path / "bot.json"
    path.write_text(json.dumps({"slack": {"bot_token": "xoxb", "app_token": "xapp"}}))
    cfg = BotConfig.load(path)
    cfg.slack.owner_id = "U7"
    cfg.slack.control_channel = "C3"
    cfg.save()
    saved = json.loads(path.read_text())
    assert saved["slack"] == {
        "bot_token": "xoxb",
        "app_token": "xapp",
        "owner_id": "U7",
        "control_channel": "C3",
    }
    assert path.stat().st_mode & 0o777 == 0o600


def test_a_slack_block_without_the_extra_fails_with_the_install_command(monkeypatch):
    import sys

    from tgforge.base.app import platform_transports

    for name in [m for m in sys.modules if m.split(".")[0] == "slack_sdk"] + ["slack_sdk"]:
        monkeypatch.setitem(sys.modules, name, None)  # the extra not installed
    monkeypatch.delitem(sys.modules, "tgforge.base.slack", raising=False)
    cfg = BotConfig(slack={"bot_token": "xoxb", "app_token": "xapp"})
    with pytest.raises(SystemExit, match=r'uv pip install "tgforge\[slack\]"'):
        platform_transports(cfg)


# ── window keys ─────────────────────────────────────────────────────


def test_a_slack_key_reaches_slack_and_a_plain_key_telegram(tmp_path):
    async def scenario():
        core = _kernel(tmp_path)
        tg, sl = _own(core, TG_WINDOW), _own(core, SLACK_WINDOW)
        await tg.send("to telegram")
        await sl.send("to slack")
        assert [text for _t, text in kernel_bot(core).sent] == ["to telegram"]
        posts = slack_client(core).of("chat_postMessage")
        assert [(p["channel"], p["text"]) for p in posts] == [("C9", "to slack")]

    asyncio.run(scenario())


def test_a_key_of_an_unconfigured_platform_has_no_transport(tmp_path):
    cfg = BotConfig(token="x", home=str(tmp_path), owner_id=1, chat_id=100)
    from tgforge.testing import MockBot, telegram_kernel

    core = telegram_kernel(MockBot(), cfg, [])
    assert core.find_transport("slack:C9") is None  # never handed to Telegram
    assert core.find_transport("1001") is core.transports[0]


def test_two_generals_keep_separate_menu_and_text_prompt_state(tmp_path):
    async def scenario():
        core = _kernel(tmp_path)
        tg_prompt = asyncio.create_task(core.ask_text("0", "Telegram?"))
        sl_prompt = asyncio.create_task(core.ask_text(SLACK_GENERAL, "Slack?"))
        menu = asyncio.create_task(core.show_menu(SLACK_GENERAL))
        for _ in range(20):
            await asyncio.sleep(0)
        assert set(core._await_input) == {"0", SLACK_GENERAL}
        assert set(core._nav) == {SLACK_GENERAL}  # the Telegram General has no menu
        await core.handle_message(
            Inbound(window=SLACK_GENERAL, message_id=None, user_id="U1", text="s")
        )
        assert await asyncio.wait_for(sl_prompt, 2) == "s"
        assert not tg_prompt.done()  # the other General still waits
        await core.handle_message(Inbound(window="0", message_id=5, user_id=1, text="t"))
        assert await asyncio.wait_for(tg_prompt, 2) == "t"
        menu.cancel()

    asyncio.run(scenario())


# ── classify ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "error, expected",
    [
        (api_error("ratelimited", 429, {"Retry-After": "7"}), Flood(seconds=7.0)),
        (api_error("internal_error", 503), Transient()),
        (aiohttp.ClientConnectionError("reset"), Transient()),
        (api_error("message_not_found"), NotFound()),
        (api_error("cant_delete_message"), Refused()),
        (api_error("message_not_modified"), NotModified()),
        (ValueError("not a platform error"), None),
    ],
    ids=["rate-limit", "5xx", "network", "not-found", "cant-delete", "not-modified", "other"],
)
def test_slack_classify_table(tmp_path, error, expected):
    adapter = slack_adapter(slack_config(str(tmp_path)))
    assert adapter.classify(error) == expected


def test_a_slack_rate_limit_waits_its_retry_seconds_then_lands(tmp_path, monkeypatch):
    async def scenario():
        client = FakeSlackClient()
        client.fail["chat_postMessage"] = [api_error("ratelimited", 429, {"Retry-After": "1"})]
        tx = Transport(slack_adapter(slack_config(str(tmp_path)), client))
        slept = []
        real_sleep = asyncio.sleep

        async def fake_sleep(seconds):
            slept.append(seconds)
            await real_sleep(0)

        monkeypatch.setattr(asyncio, "sleep", fake_sleep)
        ts = await tx.send(SLACK_WINDOW, "hello")
        assert ts is not None and len(client.of("chat_postMessage")) == 2
        assert any(0.9 <= s <= 1.0 for s in slept)  # the flood deadline it recorded

    asyncio.run(scenario())


# ── rendering ───────────────────────────────────────────────────────


def test_a_block_kit_button_round_trips_its_tap_data(tmp_path):
    async def scenario():
        core = _kernel(tmp_path)
        task = asyncio.create_task(core.ask_buttons(SLACK_GENERAL, "Pick?", [("A", "a")]))
        for _ in range(20):
            await asyncio.sleep(0)
        post = slack_client(core).of("chat_postMessage")[-1]
        actions = [b for b in post["blocks"] if b["type"] == "actions"]
        button = actions[0]["elements"][0]
        assert button["text"]["text"] == "A"
        payload = {
            "type": "block_actions",
            "user": {"id": "U1"},
            "channel": {"id": "C0"},
            "message": {"ts": "1700000001.000100"},
            "actions": [{"value": button["value"]}],
        }
        taps = core.transports[1].adapter.to_taps(payload)
        assert [t.data for t in taps] == [button["value"]]
        await core.handle_tap(taps[0])
        value, _ = await asyncio.wait_for(task, 2)
        assert value == "a"

    asyncio.run(scenario())


def test_slack_renders_markdown_as_a_markdown_block_and_plain_unparsed(tmp_path):
    async def scenario():
        client = FakeSlackClient()
        tx = Transport(slack_adapter(slack_config(str(tmp_path)), client))
        await tx.send_rich(SLACK_WINDOW, "**bold** text")
        await tx.send(SLACK_WINDOW, "*not bold*")
        rich, plain = client.of("chat_postMessage")
        assert rich["blocks"] == [{"type": "markdown", "text": "**bold** text"}]
        assert plain["mrkdwn"] is False and "blocks" not in plain

    asyncio.run(scenario())


def test_pages_on_slack_post_the_summary_then_each_page_in_its_thread(tmp_path):
    async def scenario():
        client = FakeSlackClient()
        tx = Transport(slack_adapter(slack_config(str(tmp_path)), client))
        pages = ui.Pages("🔧 3 · 💬 1", ["page one", "page two"])
        first = await tx.send_md(SLACK_WINDOW, pages)
        summary, one, two = client.of("chat_postMessage")
        assert summary["text"] == "🔧 3 · 💬 1" and "thread_ts" not in summary
        assert [one["thread_ts"], two["thread_ts"]] == [first, first]
        assert one["text"] == "↳ 1/2\n\npage one" and two["text"] == "↳ 2/2\n\npage two"

    asyncio.run(scenario())


def test_landed_pages_on_slack_edit_the_card_to_the_summary_and_thread_under_it(tmp_path):
    async def scenario():
        client = FakeSlackClient()
        tx = Transport(slack_adapter(slack_config(str(tmp_path)), client))
        card = await tx.send(SLACK_WINDOW, "working…")
        pages = ui.Pages("🔧 1", ["the timeline"])
        assert await tx.land_frame(SLACK_WINDOW, card, None, pages)
        await tx.send_md(SLACK_WINDOW, pages, first_id=card)
        edit = client.of("chat_update")[-1]
        assert edit["ts"] == card and edit["blocks"] == [{"type": "markdown", "text": "🔧 1"}]
        reply = client.of("chat_postMessage")[-1]
        assert reply["thread_ts"] == card and "the timeline" in reply["text"]

    asyncio.run(scenario())


def test_pages_on_telegram_stay_one_message_per_page(tmp_path):
    async def scenario():
        core = _kernel(tmp_path)
        tx = core.transports[0]
        await tx.send_md(TG_WINDOW, ui.Pages("🔧 2", ["one", "two"]))
        sent = kernel_bot(core).sent
        assert len(sent) == 2 and all(t == 1001 for t, _ in sent)  # both in the topic itself
        assert sent[1][1].startswith("**>↳ 2/2")

    asyncio.run(scenario())


def test_a_buttons_only_edit_on_slack_keeps_the_message_body(tmp_path):
    async def scenario():
        client = FakeSlackClient()
        tx = Transport(slack_adapter(slack_config(str(tmp_path)), client))
        ts = await tx.send_rich(
            SLACK_WINDOW, "the body", reply_markup=ui.stacked([ui.act("x", "a")])
        )
        assert await tx.set_buttons(SLACK_WINDOW, ts, None)
        update = client.of("chat_update")[-1]
        assert update["blocks"] == [{"type": "markdown", "text": "the body"}]  # buttons gone

    asyncio.run(scenario())


def test_a_photo_edit_on_slack_does_not_land(tmp_path):
    async def scenario():
        tx = Transport(slack_adapter(slack_config(str(tmp_path))))
        assert await tx.edit_photo_b64(SLACK_WINDOW, "1.1", "image/png", "AAAA") is False

    asyncio.run(scenario())


# ── threads ─────────────────────────────────────────────────────────


def test_a_thread_reply_arrives_with_its_parents_text(tmp_path):
    async def scenario():
        client = FakeSlackClient()
        client.parents["1700000050.000100"] = {"text": "the parent &amp; more", "user": "U2"}
        adapter = slack_adapter(slack_config(str(tmp_path)), client)
        event = _slack_message("about that", thread_ts="1700000050.000100")
        inbound = await adapter.to_inbound(event)
        assert inbound.window == SLACK_WINDOW and inbound.text == "about that"
        assert inbound.in_thread and inbound.replied_text == "the parent & more"
        assert inbound.replied_user_id == "U2"

    asyncio.run(scenario())


def test_a_thread_reply_is_kept_when_its_parent_cannot_be_read(tmp_path, monkeypatch):
    import tgforge.base.slack as slack_module

    warnings = []
    monkeypatch.setattr(slack_module.LOGGER, "warning", lambda msg, *a: warnings.append(msg % a))

    async def scenario():
        client = FakeSlackClient()
        client.fail["conversations_replies"] = [api_error("internal_error", 503)]
        adapter = slack_adapter(slack_config(str(tmp_path)), client)
        event = _slack_message("about that", thread_ts="1700000050.000100")
        inbound = await adapter.to_inbound(event)
        assert inbound is not None and inbound.text == "about that" and inbound.in_thread
        assert inbound.replied_text is None
        assert any("thread parent" in w for w in warnings)

    asyncio.run(scenario())


def test_a_thread_message_does_not_move_a_bottom_frame(tmp_path):
    async def scenario():
        core = _kernel(tmp_path)
        win = _own(core, SLACK_WINDOW)
        win.show_bottom_frame("panel", "jobs")
        tx = core.transports[1]
        await tx.flush_frames()
        panel = win.bottom_frame_id("panel")
        newer = slack_client(core)._next_ts()  # posted after the panel
        thread_reply = Inbound(SLACK_WINDOW, newer, "U1", "in a thread", in_thread=True)
        await core.handle_message(thread_reply)
        await tx.flush_frames()
        assert win.bottom_frame_id("panel") == panel  # not re-sent below it
        await core.handle_message(Inbound(SLACK_WINDOW, newer, "U1", "in the channel"))
        await tx.flush_frames()
        assert win.bottom_frame_id("panel") != panel  # a channel message does move it

    asyncio.run(scenario())


# ── Socket Mode ─────────────────────────────────────────────────────


class _Request:
    def __init__(self, kind, payload):
        self.envelope_id = "env-1"
        self.type = kind
        self.payload = payload


def test_an_envelope_is_acknowledged_before_its_handler_runs(tmp_path):
    async def scenario():
        order = []
        adapter = slack_adapter(slack_config(str(tmp_path)))

        class _Socket:
            async def send_socket_mode_response(self, response):
                order.append(("ack", response.envelope_id))

        async def on_message(inbound):
            order.append(("handled", inbound.text))

        payload = {"event": _slack_message("hi")}
        await adapter.on_request(_Socket(), _Request("events_api", payload), on_message, None)
        await asyncio.gather(*adapter._handlers)
        assert order == [("ack", "env-1"), ("handled", "hi")]

    asyncio.run(scenario())


def test_the_bots_own_posts_and_edits_are_not_inbound(tmp_path):
    async def scenario():
        adapter = slack_adapter(slack_config(str(tmp_path)))
        assert await adapter.to_inbound(_slack_message("mine", bot_id="BX")) is None
        assert await adapter.to_inbound(_slack_message("x", subtype="message_changed")) is None
        mention = await adapter.to_inbound(_slack_message("<@B0> /menu"))
        assert mention.mentioned and mention.text.strip() == "/menu"

    asyncio.run(scenario())


# ── the /tgf command ────────────────────────────────────────────────


def _command(adapter, text, channel="C9"):
    return adapter.command_to_inbound(
        {"command": "/tgf", "text": text, "channel_id": channel, "user_id": "U1"}
    )


@pytest.mark.parametrize(
    "typed, seen",
    [
        ("model opus", ("model", "opus")),  # a class command with its args
        ("/usage2", ("unknown", "/usage2")),  # the `//` escape skips the universal tier
        ("frobnicate now", ("unknown", "/frobnicate now")),  # an unknown skill falls through
    ],
    ids=["class-command", "escape", "unknown-skill"],
)
def test_tgf_routes_like_the_telegram_slash(tmp_path, typed, seen):
    async def scenario():
        core = _kernel(tmp_path)
        _own(core, SLACK_WINDOW)
        _own(core, TG_WINDOW)
        slack_text = _command(core.transports[1].adapter, typed).text
        await core.handle_message(_command(core.transports[1].adapter, typed))
        await core.handle_message(Inbound(TG_WINDOW, 9, 1, slack_text))  # the same, typed
        assert SEEN == [seen, seen]
        echo = slack_client(core).of("chat_postMessage")[0]
        assert echo["text"] == f"› {slack_text}"  # the command shown before it runs

    asyncio.run(scenario())


def test_a_bare_tgf_opens_the_menu(tmp_path):
    async def scenario():
        core = _kernel(tmp_path)
        _own(core, SLACK_WINDOW)
        task = asyncio.create_task(core.handle_message(_command(core.transports[1].adapter, "")))
        for _ in range(20):
            await asyncio.sleep(0)
        posts = slack_client(core).of("chat_postMessage")
        assert posts[0]["text"] == "› /"
        assert any(b["type"] == "actions" for b in posts[-1]["blocks"])  # the menu's buttons
        task.cancel()

    asyncio.run(scenario())


# ── owners and auth ─────────────────────────────────────────────────


def test_the_owner_check_runs_per_platform(tmp_path):
    core = _kernel(tmp_path)
    auth = OwnerOnly()
    assert auth(core, Inbound(SLACK_WINDOW, None, "U1", "x"))
    assert not auth(core, Inbound(SLACK_WINDOW, None, 1, "x"))  # Telegram's owner, on Slack
    assert auth(core, Inbound(TG_WINDOW, None, 1, "x"))
    assert not auth(core, Inbound(TG_WINDOW, None, "U1", "x"))  # Slack's owner, on Telegram
    assert not auth(core, Tap(SLACK_WINDOW, None, "d", "U2"))


def test_is_admin_compares_against_the_windows_platform_owner(tmp_path):
    core = _kernel(tmp_path)
    assert core._ctx(
        "work", Inbound(SLACK_WINDOW, None, "U1", ""), SLACK_WINDOW, "", "", None
    ).is_admin
    assert not core._ctx(
        "work", Inbound(SLACK_WINDOW, None, 1, ""), SLACK_WINDOW, "", "", None
    ).is_admin


def test_an_auth_hook_sees_a_slack_event(tmp_path):
    allow = Allowlist()
    core = _kernel(tmp_path, [allow])
    auth = AllowlistAuth(allow)
    assert not auth(core, Inbound(SLACK_WINDOW, None, "U5", "x"))
    allow.saved["users"] = ["U5"]
    assert auth(core, Inbound(SLACK_WINDOW, None, "U5", "x"))
    assert not auth(core, Inbound(TG_WINDOW, None, 7, "x"))  # not Telegram's owner, not listed


def test_init_on_slack_binds_the_slack_owner_only(tmp_path):
    async def scenario():
        cfg = slack_config(str(tmp_path), owner_id=None)
        core = two_platform_kernel(cfg, [])
        await core.handle_message(Inbound(SLACK_GENERAL, "1.1", "U9", "/init", mentioned=True))
        assert cfg.slack.owner_id == "U9" and cfg.owner_id == 1  # Telegram's untouched
        invite = slack_client(core).of("conversations_invite")
        assert invite == [{"channel": "C0", "users": "U9"}]  # into the control channel

    asyncio.run(scenario())


# ── channels ────────────────────────────────────────────────────────


def test_a_title_slugs_to_a_channel_name():
    assert channel_slug("🤖 Claude · Fix the Login Bug!") == "claude-fix-the-login-bug"
    assert channel_slug("🖥 Shell #2") == "shell-2"
    assert channel_slug("✨✨") == "window"
    assert len(channel_slug("x" * 200)) == NAME_MAX


def test_a_channel_name_clash_takes_a_suffix_and_the_topic_keeps_the_title(tmp_path):
    async def scenario():
        client = FakeSlackClient()
        client.fail["conversations_create"] = [api_error("name_taken")]
        adapter = slack_adapter(slack_config(str(tmp_path)), client)
        window = await adapter.create_window("🖥 Shell")
        names = [kw["name"] for kw in client.of("conversations_create")]
        assert names == ["shell", "shell-2"]
        assert client.of("conversations_create")[-1]["is_private"] is True
        assert client.of("conversations_setTopic") == [{"channel": "C1", "topic": "🖥 Shell"}]
        assert window == "slack:C1"
        assert client.of("conversations_invite") == [{"channel": "C1", "users": "U1"}]

    asyncio.run(scenario())


def test_closing_a_slack_window_archives_its_channel(tmp_path):
    async def scenario():
        core = _kernel(tmp_path)
        win = _own(core, SLACK_WINDOW)
        await win.close()
        assert slack_client(core).of("conversations_archive") == [{"channel": "C9"}]
        assert SLACK_WINDOW not in core.owners

    asyncio.run(scenario())


def test_the_control_channel_is_created_once_and_saved(tmp_path):
    async def scenario():
        path = tmp_path / "bot.json"
        path.write_text(json.dumps({"slack": {"bot_token": "xoxb", "app_token": "xapp"}}))
        cfg = BotConfig.load(path)
        adapter = slack_adapter(cfg)
        assert await adapter.identity() == "mybot"
        assert cfg.slack.control_channel == "C1"
        assert json.loads(path.read_text())["slack"]["control_channel"] == "C1"
        await adapter.identity()
        assert len(adapter.client.of("conversations_create")) == 1  # not created again

    asyncio.run(scenario())


# ── the Claude window on Slack ──────────────────────────────────────


def test_the_session_note_quotes_the_windows_platform_and_limits(tmp_path):
    from tgforge.plugins.claude import Claude, ClaudeTopic

    core = _kernel(tmp_path, [Claude()])
    slack = _own(core, SLACK_WINDOW, ClaudeTopic)
    telegram = _own(core, TG_WINDOW, ClaudeTopic)
    slack_note, telegram_note = slack._session_note(), telegram._session_note()
    assert slack_note.startswith("[via Slack bot") and "Telegram" not in slack_note
    assert "1GB Slack download cap" in slack_note
    assert telegram_note.startswith("[via Telegram bot") and "20MB Telegram download cap" in (
        telegram_note
    )
    assert "Telegram renders them as raw text" in telegram_note


def test_a_claude_session_gets_its_window_key_in_the_env(tmp_path):
    from tgforge.plugins.claude import Claude, ClaudeTopic

    async def scenario():
        core = _kernel(tmp_path, [Claude()])
        win = _own(core, SLACK_WINDOW, ClaudeTopic)
        win.workspace = str(tmp_path)
        captured = {}

        async def spawn(*args, **kwargs):
            captured.update(kwargs["env"])
            raise RuntimeError("stop before the CLI")

        win.spawn = spawn
        with pytest.raises(RuntimeError):
            await win._ensure_proc()
        assert captured["TGFORGE_WINDOW"] == SLACK_WINDOW
        assert captured["TGFORGE_BOT_DRIVEN"] == "1"

    asyncio.run(scenario())


def test_attachments_download_into_the_attachments_dir(tmp_path):
    from tgforge.base.kernel import Attachment
    from tgforge.plugins.claude import Claude, ClaudeTopic

    async def scenario():
        core = _kernel(tmp_path, [Claude()])
        win = _own(core, SLACK_WINDOW, ClaudeTopic)
        win.workspace = str(tmp_path)
        got = []

        async def download(attachment, dest: Path):
            got.append(dest)
            return dest

        core.transports[1].adapter.download = download
        paths = await win._download_attachments([Attachment("notes.txt", "document", "url")])
        assert paths == got and got[0].parent == tmp_path / "tmp" / "attachments"
        assert got[0].name.endswith("__notes.txt")

    asyncio.run(scenario())


def test_the_rich_body_is_a_slack_markdown_value(tmp_path):
    adapter = slack_adapter(slack_config(str(tmp_path)))
    rich, plain = adapter.render("# hi")
    assert rich == SlackMarkdown(markdown="# hi") and plain == "# hi"
    assert adapter.render("x" * 12001)[0] is None  # over the block cap: plain goes instead


def test_the_help_text_quotes_each_platforms_command_prefix(tmp_path):
    core = _kernel(tmp_path)
    _own(core, SLACK_WINDOW)
    _own(core, TG_WINDOW)
    slack, telegram = core.help_text(SLACK_WINDOW), core.help_text(TG_WINDOW)
    assert slack.startswith("Type /tgf (or /tgf menu) for the button menu.")
    assert "  /tgf model — switch the model" in slack and "\n  /model" not in slack
    assert telegram.startswith("Tap / (or /menu) for the button menu.")
    assert "  /model — switch the model" in telegram


def test_an_upload_with_no_reported_id_still_moves_the_bottom_frame_below_it(tmp_path):
    async def scenario():
        core = _kernel(tmp_path)
        win = _own(core, SLACK_WINDOW)
        win.show_bottom_frame("panel", "jobs")
        tx = core.transports[1]
        await tx.flush_frames()
        panel = win.bottom_frame_id("panel")
        doc = tmp_path / "report.txt"
        doc.write_text("data")
        await win.send_file(doc)  # the fake upload reports no `shares`
        await tx.flush_frames()
        assert slack_client(core).of("files_upload_v2")
        assert win.bottom_frame_id("panel") != panel  # re-sent below the file
        assert slack_client(core).of("chat_delete") == [{"channel": "C9", "ts": panel}]

    asyncio.run(scenario())


def test_a_refused_upload_leaves_the_bottom_frame_in_place(tmp_path):
    async def scenario():
        core = _kernel(tmp_path)
        win = _own(core, SLACK_WINDOW)
        win.show_bottom_frame("panel", "jobs")
        tx = core.transports[1]
        await tx.flush_frames()
        panel = win.bottom_frame_id("panel")
        slack_client(core).fail["files_upload_v2"] = [api_error("not_allowed")]
        doc = tmp_path / "report.txt"
        doc.write_text("data")
        await win.send_file(doc)
        await tx.flush_frames()
        assert win.bottom_frame_id("panel") == panel  # nothing landed below it

    asyncio.run(scenario())


def test_allow_by_slack_mention(tmp_path):
    async def scenario():
        allow = Allowlist()
        core = _kernel(tmp_path, [allow])
        _own(core, SLACK_WINDOW)
        adapter = core.transports[1].adapter
        inbound = await adapter.to_inbound(_slack_message("<@B0> /allow <@U5|pat> <@U5>"))
        assert inbound.mentioned_user_ids == ["U5"]  # the bot left out, no repeats
        await core.handle_message(inbound)
        assert allow.allowed() == {"U5"}

    asyncio.run(scenario())


def test_landing_a_pages_card_renders_only_its_first_message(tmp_path):
    async def scenario():
        core = _kernel(tmp_path)
        tx = core.transports[0]
        rendered = []
        real = tx.adapter.render_pages

        def counting(pages):
            rendered.append(len(pages.pages))
            return real(pages)

        tx.adapter.render_pages = counting
        card = await tx.send(TG_WINDOW, "working…")
        pages = ui.Pages("🔧 9", [f"page {i}" for i in range(9)])
        assert await tx.land_frame(TG_WINDOW, card, None, pages)
        assert rendered and all(n == 1 for n in rendered)  # the record and the edit: page 1 only

    asyncio.run(scenario())


def test_window_names_dedup_within_one_platform_only(tmp_path):
    async def scenario():
        core = _kernel(tmp_path, [Work()])
        telegram = await core.open_window(WorkTopic, "Notes", origin="0")
        slack = await core.open_window(WorkTopic, "Notes", origin=SLACK_GENERAL)
        again = await core.open_window(WorkTopic, "Notes", origin=SLACK_GENERAL)
        assert (telegram.name, slack.name, again.name) == ("Notes", "Notes", "Notes #2")

    asyncio.run(scenario())


def test_a_windows_adapter_is_its_platforms(tmp_path):
    core = _kernel(tmp_path)
    assert _own(core, SLACK_WINDOW).adapter is core.transports[1].adapter
    assert _own(core, TG_WINDOW).adapter is core.transports[0].adapter


def test_a_failed_socket_connect_still_closes_the_client(tmp_path, monkeypatch):
    import tgforge.base.slack as slack_module

    closed = []

    class _Socket:
        def __init__(self, app_token, web_client):
            self.socket_mode_request_listeners = []

        async def connect(self):
            raise aiohttp.ClientConnectionError("refused")

        async def close(self):
            closed.append(True)

    monkeypatch.setattr(slack_module, "SocketModeClient", _Socket)
    adapter = slack_adapter(slack_config(str(tmp_path)))
    with pytest.raises(aiohttp.ClientConnectionError):
        asyncio.run(adapter.run(None, None))
    assert closed == [True]


def _interrupted_card(core, window, card):
    from tgforge.plugins.claude import ClaudeTopic

    t = _own(core, window, ClaudeTopic)
    t.turn_open = True
    t.turn.card_id = card
    t.turn.timeline = [("tool", "💻 Bash: ls")]
    t._summary = lambda *a, **k: "🔧 1 · 3s"
    return t


def test_an_interrupted_card_on_slack_keeps_its_timeline_in_the_cards_thread(tmp_path):
    from tgforge.plugins.claude import Claude

    async def scenario():
        core = _kernel(tmp_path, [Claude()])
        client = slack_client(core)
        card = await core.transports[1].send(SLACK_WINDOW, "working…")
        await _interrupted_card(core, SLACK_WINDOW, card).on_shutdown()
        edit = client.of("chat_update")[-1]
        assert edit["ts"] == card and "interrupted by a restart" in edit["text"]
        reply = client.of("chat_postMessage")[-1]
        assert reply["thread_ts"] == card and "💻 Bash: ls" in reply["text"]

    asyncio.run(scenario())


def test_an_interrupted_card_on_telegram_sends_nothing_extra(tmp_path):
    from tgforge.plugins.claude import Claude

    async def scenario():
        core = _kernel(tmp_path, [Claude()])
        bot = kernel_bot(core)
        card = await core.transports[0].send(TG_WINDOW, "working…")
        sent = len(bot.sent)
        await _interrupted_card(core, TG_WINDOW, card).on_shutdown()
        assert len(bot.sent) == sent  # page 1 rides the card itself
        assert "interrupted by a restart" in bot.edits[-1][1]

    asyncio.run(scenario())
