"""Mermaid code blocks go out on Telegram as photos inside the Rich Message, where the
block stood; a diagram that does not draw stays code with a fallback notice; the
later forms keep the code; the splitter counts each diagram against the
Rich limits; the renderer keeps one browser open and closes it once idle."""

from __future__ import annotations

import asyncio
import hashlib
import os
import struct
import sys
import textwrap
import time
from pathlib import Path

import pytest
from aiogram.exceptions import TelegramBadRequest

from tgforge.base import mermaid
from tgforge.base.kernel import NOTICE_FALLBACK
from tgforge.base.mermaid import DiagramError, DiagramFiles, MermaidRenderer, open_browser_page
from tgforge.base.slack import SlackAdapter
from tgforge.base.telegram import rich_blocks, rich_fits, rich_html
from tgforge.testing import MockBot, TestClient

WINDOW = "555"


class _FakeRenderer:
    """Draws each source as `PNG:<source>`; a source in `refuse` raises with its cause."""

    def __init__(self, refuse=None):
        self.refuse = refuse or {}
        self.drawn: list[str] = []

    async def draw(self, source: str) -> bytes:
        self.drawn.append(source)
        if source in self.refuse:
            raise DiagramError(self.refuse[source])
        return f"PNG:{source}".encode()


def _client(bot=None, refuse=None):
    c = TestClient()
    if bot is not None:
        c.bot = bot
        c.adapter.bot = bot
    c.adapter.diagrams = _FakeRenderer(refuse)
    tx = c.core.transports[0]
    notices = []
    tx.on_notice = notices.append
    return c, tx, notices


def _block(source: str) -> str:
    return f"```mermaid\n{source}\n```"


def test_a_mermaid_block_goes_out_as_a_photo_where_it_stood():
    async def scenario():
        c, tx, notices = _client()
        mid = await tx.send_rich(WINDOW, f"before\n\n{_block('graph TD\na-->b')}\n\nafter")
        return c, mid, notices

    c, mid, notices = asyncio.run(scenario())
    assert c.bot.rich[mid] == '<p>before</p><img src="tg://photo?id=diagram1"/><p>after</p>'
    assert c.bot.rich_media[mid] == [("diagram1", b"PNG:graph TD\na-->b")]
    assert c.adapter.diagrams.drawn == ["graph TD\na-->b"]  # the source unescaped
    assert notices == []


def test_several_diagrams_each_take_their_place_in_order():
    async def scenario():
        c, tx, _notices = _client()
        in_item = textwrap.indent(_block("graph LR\nx-->y"), "  ")
        text = f"{_block('graph TD\na-->b')}\n\nmiddle\n\n- item\n\n{in_item}"
        return c, await tx.send_rich(WINDOW, text)

    c, mid = asyncio.run(scenario())
    html = c.bot.rich[mid]
    first, second = html.index("tg://photo?id=diagram1"), html.index("tg://photo?id=diagram2")
    assert first < html.index("middle") < second
    assert "language-mermaid" not in html
    assert [media_id for media_id, _png in c.bot.rich_media[mid]] == ["diagram1", "diagram2"]
    assert c.adapter.diagrams.drawn == ["graph TD\na-->b", "graph LR\nx-->y"]


def test_a_diagram_that_does_not_draw_stays_code_and_raises_a_fallback_notice():
    cause = "Mermaid could not draw it: Parse error on line 2"

    async def scenario():
        c, tx, notices = _client(refuse={"graph TD\na-->": cause})
        text = f"{_block('graph TD\na-->')}\n\n{_block('graph TD\nb-->c')}"
        return c, await tx.send_rich(WINDOW, text), notices

    c, mid, notices = asyncio.run(scenario())
    html = c.bot.rich[mid]
    assert html.startswith('<pre><code class="language-mermaid">graph TD\na--&gt;</code></pre>')
    assert html.endswith('<img src="tg://photo?id=diagram1"/>')  # the other one still draws
    assert [(n.kind, n.window, n.what, n.forms, n.error, n.size) for n in notices] == [
        (NOTICE_FALLBACK, WINDOW, "diagram", "image → code", cause, len("graph TD\na-->"))
    ]


def test_mermaid_support_is_in_the_default_install():
    import tomllib
    from importlib import metadata

    project = tomllib.loads((Path(__file__).parent.parent / "pyproject.toml").read_text())
    assert "playwright~=1.64.0" in project["project"]["dependencies"]
    assert "mermaid" not in project["project"]["optional-dependencies"]
    assert "playwright~=1.64.0" in metadata.requires("tgforge")  # no extra marker
    package = Path(mermaid.__file__).parent
    assert not list(package.glob("mermaid*.js")) and not list(package.glob("*LICENSE*"))


class _RichRefusingBot(MockBot):
    async def send_rich_message(self, chat_id, rich_message, **kw):
        raise TelegramBadRequest(method=None, message="Bad Request: can't parse rich message")


def test_the_later_forms_keep_the_block_as_code():
    async def scenario():
        c, tx, notices = _client(bot=_RichRefusingBot())
        return c, await tx.send_rich(WINDOW, _block("graph TD\na-->b")), notices

    c, mid, notices = asyncio.run(scenario())
    assert c.bot.forms[mid] == "plain"
    assert c.bot.created[-1][2].startswith("```mermaid\ngraph TD\na-->b")
    assert [(n.kind, n.forms) for n in notices] == [(NOTICE_FALLBACK, "Rich with images → plain")]


def test_an_edit_draws_its_diagrams_too():
    async def scenario():
        c, tx, _notices = _client()
        mid = await tx.send_rich(WINDOW, "draft")
        await tx.edit_rich(WINDOW, mid, f"final\n\n{_block('graph TD\na-->b')}")
        return c, mid

    c, mid = asyncio.run(scenario())
    assert c.bot.rich[mid] == '<p>final</p><img src="tg://photo?id=diagram1"/>'
    assert c.bot.rich_media[mid] == [("diagram1", b"PNG:graph TD\na-->b")]


def test_slack_keeps_the_block_as_code():
    body = object()
    assert asyncio.run(SlackAdapter.draw_diagrams(None, "slack:C1", body)) is body


def test_an_image_counts_as_a_block_and_as_one_of_the_fifty_media():
    assert rich_blocks('<img src="tg://photo?id=diagram1"/>') == 1
    fifty = "".join(f'<img src="tg://photo?id=d{i}"/>' for i in range(50))
    assert rich_fits(fifty)
    assert not rich_fits(fifty + '<img src="tg://photo?id=d50"/>')


def test_the_splitter_keeps_each_piece_within_fifty_diagrams():
    text = "\n\n".join(_block(f"graph TD\na{i}-->b") for i in range(60))
    assert len(rich_html(text).encode()) < 32768  # under the byte limit: media decide

    async def scenario():
        c, tx, _notices = _client()
        await tx.send_rich(WINDOW, text)
        return c

    c = asyncio.run(scenario())
    per_message = [len(media) for media in c.bot.rich_media.values()]
    assert sum(per_message) == 60
    assert len(per_message) == 2 and max(per_message) <= 50


def test_a_diagram_drawn_into_a_message_fits_when_its_code_did():
    code = rich_html(_block("graph TD\na-->b"))
    drawn = '<img src="tg://photo?id=diagram1"/>'
    assert rich_blocks(drawn) <= rich_blocks(code) and len(drawn) <= len(code)


# ── The renderer ───────────────────────────────────────────────────


class _FakePage:
    def __init__(self, delay=0.0, fail=None):
        self.delay = delay
        self.fail = fail
        self.closed = False
        self.drawn: list[str] = []

    async def draw(self, source, serial):
        await asyncio.sleep(self.delay)
        if self.fail is not None:
            raise self.fail
        self.drawn.append(source)
        return b"PNG"

    async def close(self):
        self.closed = True


def _renderer(monkeypatch, pages, **kw):
    """A renderer on stand-in pages, its files checking out."""
    monkeypatch.setattr(mermaid, "files_ready", lambda files: True)
    opened = []

    async def open_page(files):
        page = pages.pop(0)
        opened.append(page)
        return page

    files = DiagramFiles(Path("bundle.js"), Path("browsers"))
    return MermaidRenderer(files, open_page=open_page, **kw), opened


def test_the_renderer_keeps_one_page_across_diagrams_and_closes_it_once_idle(monkeypatch):
    async def scenario():
        renderer, opened = _renderer(monkeypatch, [_FakePage(), _FakePage()], idle_close=0.2)
        await renderer.draw("a")
        await asyncio.sleep(0.1)
        await renderer.draw("b")  # within the idle time: the same page, its timer reset
        await asyncio.sleep(0.15)
        open_at_250ms = renderer.is_open
        await asyncio.sleep(0.15)
        closed_once_idle = not renderer.is_open
        await renderer.draw("c")  # a fresh page after the idle close
        await renderer.close()
        return opened, open_at_250ms, closed_once_idle

    opened, open_at_250ms, closed_once_idle = asyncio.run(scenario())
    assert open_at_250ms and closed_once_idle
    assert [page.drawn for page in opened] == [["a", "b"], ["c"]]
    assert [page.closed for page in opened] == [True, True]


def test_a_timeout_closes_the_page_and_names_the_cause(monkeypatch):
    async def scenario():
        renderer, opened = _renderer(monkeypatch, [_FakePage(delay=5), _FakePage()], timeout=0.05)
        with pytest.raises(DiagramError, match="timed out after 0 s"):
            await renderer.draw("slow")
        closed = opened[0].closed and not renderer.is_open
        await renderer.draw("next")
        await renderer.close()
        return closed, opened

    closed, opened = asyncio.run(scenario())
    assert closed
    assert opened[1].drawn == ["next"]


def test_a_browser_failure_closes_the_page_and_a_refused_source_keeps_it(monkeypatch):
    async def scenario():
        renderer, opened = _renderer(
            monkeypatch, [_FakePage(fail=DiagramError("Mermaid could not draw it"))]
        )
        with pytest.raises(DiagramError, match="Mermaid could not draw it"):
            await renderer.draw("bad")
        kept = renderer.is_open
        opened[0].fail = RuntimeError("Target page, context or browser has been closed")
        with pytest.raises(DiagramError, match="the headless browser failed: Target page"):
            await renderer.draw("any")
        return kept, renderer.is_open, opened[0].closed

    kept, still_open, closed = asyncio.run(scenario())
    assert kept and not still_open and closed


def test_a_missing_browser_is_named(tmp_path):
    files = DiagramFiles(tmp_path / "bundle.js", tmp_path / "browsers")
    with pytest.raises(DiagramError) as caught:
        asyncio.run(open_browser_page(files))
    assert str(caught.value) == f"the headless browser is missing from {tmp_path / 'browsers'}"


# ── The real browser, on the fetched bundle ────────────────────────


@pytest.fixture(scope="module")
def real_files(tmp_path_factory):
    """The pinned bundle fetched from its URL, and the browser Playwright already has
    (its default directory, or PLAYWRIGHT_BROWSERS_PATH); skipped when either is not
    to be had here."""
    bundle = tmp_path_factory.mktemp("diagram-files") / "mermaid.min.js"
    try:
        mermaid.fetch_bundle(bundle)
    except DiagramError as error:
        pytest.skip(str(error))
    default = "Library/Caches/ms-playwright" if sys.platform == "darwin" else ".cache/ms-playwright"
    browsers = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or Path.home() / default)
    if mermaid.headless_shell(browsers) is None:
        pytest.skip(f"no headless browser in {browsers}")
    return DiagramFiles(bundle, browsers)


_LABELS = 'flowchart LR\n  n0["BS<sub>1</sub><br>supersystem"] -->|"acts on"| n1["BS<sub>2</sub>"]'


def _png_size(png: bytes) -> tuple[int, int]:
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    return struct.unpack(">II", png[16:24])


def test_the_real_browser_draws_sub_labels_without_holding_the_event_loop(tmp_path, real_files):
    assert mermaid.bundle_ok(real_files.bundle)  # the fetched copy, at the pinned sha256

    async def scenario():
        renderer = MermaidRenderer(real_files)
        gaps: list[float] = []

        async def tick():
            last = time.monotonic()
            while True:
                await asyncio.sleep(0.01)
                now = time.monotonic()
                gaps.append(now - last)
                last = now

        ticker = asyncio.create_task(tick())
        try:
            cold = await renderer.draw(_LABELS)
            warm = await renderer.draw(_LABELS)
        finally:
            ticker.cancel()
            await renderer.close()
        return cold, warm, max(gaps)

    cold, warm, longest_hold = asyncio.run(scenario())
    (tmp_path / "bs-sub-labels.png").write_bytes(warm)
    width, height = _png_size(warm)
    assert cold == warm
    assert width + height <= mermaid.PHOTO_SIDES_MAX
    assert max(width, height) / min(width, height) <= mermaid.PHOTO_RATIO_MAX
    assert longest_hold < 0.1, longest_hold  # section 1's 100 ms hold bar


def test_a_huge_diagram_is_shrunk_and_padded_within_the_photo_limits(real_files):
    chain = "flowchart LR\n" + "\n".join(f"  n{i}[node {i}] --> n{i + 1}" for i in range(120))

    async def scenario():
        renderer = MermaidRenderer(real_files)
        try:
            return await renderer.draw(chain)
        finally:
            await renderer.close()

    width, height = _png_size(asyncio.run(scenario()))
    assert width + height <= mermaid.PHOTO_SIDES_MAX
    assert width / height <= mermaid.PHOTO_RATIO_MAX


# ── Review fixes: send order, a refused drawn message, the renderer's edges ──


class _SlowRenderer:
    async def draw(self, source):
        await asyncio.sleep(1.0)
        return b"PNG"


def test_a_message_keeps_its_place_in_its_window_while_its_diagram_draws():
    """The reviewer's input: window 100 sends a diagram, then a second message; window
    200 sends meanwhile. Window 100 keeps its order, window 200 does not wait."""

    async def scenario():
        c = TestClient()
        c.adapter.diagrams = _SlowRenderer()
        tx = c.core.transports[0]
        started = time.monotonic()
        landed = {}

        async def send(window, text, label):
            await tx.send_rich(window, text)
            landed[label] = time.monotonic() - started

        await asyncio.gather(
            send("100", "diagram\n\n```mermaid\ngraph TD\na-->b\n```", "diagram"),
            send("200", "plain text", "other window"),
            send("100", "second message", "second"),
        )
        return c, landed

    c, landed = asyncio.run(scenario())
    window_100 = [text[:14] for _mid, thread, text in c.bot.created if thread == 100]
    assert window_100 == ["diagram", "second message"]
    assert landed["other window"] < 0.5 <= landed["diagram"] <= landed["second"]


def test_a_shell_window_keeps_its_order_while_its_diagram_draws(tmp_path):
    from tgforge.plugins.shell import Shell, ShellTopic

    async def scenario():
        c = TestClient(Shell(), home=str(tmp_path))
        c.adapter.diagrams = _SlowRenderer()
        shell = c.core._instantiate(ShellTopic, "777", "sh")
        c.core.owners["777"] = "shell"
        await asyncio.gather(
            shell.send_rich("diagram\n\n```mermaid\ngraph TD\na-->b\n```"),
            shell.send("second message"),
        )
        return [text[:14] for _mid, thread, text in c.bot.created if thread == 777]

    assert asyncio.run(scenario()) == ["diagram", "second message"]


class _MediaRefusingBot(MockBot):
    async def send_rich_message(self, chat_id, rich_message, **kw):
        if rich_message.media:
            raise TelegramBadRequest(method=None, message="Bad Request: media not allowed here")
        return await super().send_rich_message(chat_id, rich_message, **kw)


def test_a_refused_drawn_message_goes_out_as_rich_with_its_diagrams_as_code():
    """The reviewer's input: math, a subscript and a diagram in a list item; Telegram
    refuses the message only with media."""
    text = (
        "Energy $E = mc^2$ and H<sub>2</sub>O\n\n- step\n\n  ```mermaid\n  graph TD\n  a-->b\n  ```"
    )

    async def scenario():
        c, tx, notices = _client(bot=_MediaRefusingBot())
        return c, await tx.send_rich(WINDOW, text), notices

    c, mid, notices = asyncio.run(scenario())
    html = c.bot.rich[mid]
    assert c.bot.forms[mid] == "rich" and c.bot.rich_media[mid] == []
    assert "<tg-math>E = mc^2</tg-math>" in html and "H<sub>2</sub>O" in html
    assert '<code class="language-mermaid">graph TD\na--&gt;b</code>' in html
    assert [(n.kind, n.forms) for n in notices] == [(NOTICE_FALLBACK, "Rich with images → Rich")]


def test_closing_the_adapter_closes_the_diagram_browser():
    from types import SimpleNamespace

    closed = []

    class _Renderer:
        async def close(self):
            closed.append("browser")

    async def scenario():
        c = TestClient()
        c.adapter.diagrams = _Renderer()

        async def close_session():
            closed.append("session")

        c.bot.session = SimpleNamespace(close=close_session)
        await c.adapter.close()

    asyncio.run(scenario())
    assert closed == ["browser", "session"]


def test_a_png_over_a_photos_bytes_is_refused_with_its_size(monkeypatch):
    from types import SimpleNamespace

    from tgforge.base.mermaid import BrowserPage

    monkeypatch.setattr(mermaid, "PHOTO_BYTES_MAX", 10)

    async def evaluate(script, args):
        return {}

    async def screenshot(type):
        return b"x" * 11

    page = SimpleNamespace(
        evaluate=evaluate, locator=lambda selector: SimpleNamespace(screenshot=screenshot)
    )
    with pytest.raises(DiagramError, match="its PNG is 11 bytes, over a photo's 10"):
        asyncio.run(BrowserPage(None, None, page).draw("graph TD\na-->b", 1))


def test_an_idle_close_waiting_on_a_draw_skips_once_that_draw_ran(monkeypatch):
    async def scenario():
        renderer, opened = _renderer(monkeypatch, [_FakePage()], idle_close=60)
        await renderer.draw("a")
        renderer._idle_expired()  # the timer fires just as the next draw starts
        await renderer.draw("b")
        for _ in range(5):
            await asyncio.sleep(0)
        open_after = renderer.is_open
        await renderer.close()
        return open_after, opened

    open_after, opened = asyncio.run(scenario())
    assert open_after  # the next diagram finds the browser warm
    assert opened[0].drawn == ["a", "b"]


def test_the_diagram_page_fetches_no_url(real_files):
    import http.server
    import threading

    requests: list[str] = []

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_port}/probe"

    async def scenario():
        page = await open_browser_page(real_files)
        try:
            return await page.page.evaluate(
                "url => fetch(url).then(() => 'fetched', () => 'blocked')", url
            )
        finally:
            await page.close()

    try:
        outcome = asyncio.run(scenario())
    finally:
        server.shutdown()
        server.server_close()
    assert outcome == "blocked" and requests == []


# ── Fetching the diagram files ────────────────────────────────────


class _Served:
    """A local HTTP server answering every path with `body` (`hits` counts requests)."""

    def __init__(self, body: bytes):
        import http.server
        import threading

        served = self
        self.hits = 0

        class _Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                served.hits += 1
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/mermaid.min.js"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def test_a_fetched_bundle_with_the_pinned_sha256_is_kept(tmp_path, monkeypatch):
    body = b"mermaid bundle"
    monkeypatch.setattr(mermaid, "MERMAID_SHA256", hashlib.sha256(body).hexdigest())
    served = _Served(body)
    try:
        mermaid.fetch_bundle(tmp_path / "home" / "bundle.js", served.url)
    finally:
        served.close()
    assert (tmp_path / "home" / "bundle.js").read_bytes() == body
    assert mermaid.bundle_ok(tmp_path / "home" / "bundle.js")
    assert [p.name for p in (tmp_path / "home").iterdir()] == ["bundle.js"]  # no part file


def test_a_bundle_whose_sha256_differs_is_never_written_and_a_changed_one_is_deleted(tmp_path):
    served = _Served(b"not the pinned bundle")
    try:
        with pytest.raises(DiagramError, match="has sha256 .* not the pinned"):
            mermaid.fetch_bundle(tmp_path / "bundle.js", served.url)
    finally:
        served.close()
    assert not (tmp_path / "bundle.js").exists()
    (tmp_path / "bundle.js").write_bytes(b"tampered")
    assert not mermaid.bundle_ok(tmp_path / "bundle.js")
    assert not (tmp_path / "bundle.js").exists()  # deleted, never loaded


def test_a_bundle_the_network_refuses_is_named():
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]  # closed once the block ends: nothing listens
    with pytest.raises(DiagramError, match="mermaid.js could not be fetched: .*refused"):
        mermaid.fetch_bundle(Path("unused.js"), f"http://127.0.0.1:{port}/mermaid.min.js")


def test_the_browser_fetch_installs_into_the_bot_home_or_names_its_failure(tmp_path, monkeypatch):
    import subprocess

    calls = []

    def install(command, env, **kw):
        calls.append((command[-3:], env["PLAYWRIGHT_BROWSERS_PATH"]))
        if len(calls) == 1:
            return subprocess.CompletedProcess(command, 1, "", "Error: download failed: 503\n")
        shell = Path(env["PLAYWRIGHT_BROWSERS_PATH"])
        shell = shell / f"chromium_headless_shell-{mermaid._shell_revision()}" / "linux"
        shell.mkdir(parents=True)
        (shell / "chrome-headless-shell").write_text("")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(mermaid.subprocess, "run", install)
    browsers = tmp_path / "browsers"
    with pytest.raises(DiagramError, match="could not be fetched: Error: download failed: 503"):
        mermaid.fetch_browser(browsers)
    mermaid.fetch_browser(browsers)
    assert calls == [(["playwright", "install", "chromium-headless-shell"], str(browsers))] * 2
    assert mermaid.headless_shell(browsers).name == "chrome-headless-shell"


def test_fetch_files_fetches_only_what_is_missing(tmp_path, monkeypatch):
    fetched = []
    monkeypatch.setattr(mermaid, "bundle_ok", lambda bundle: False)
    monkeypatch.setattr(mermaid, "headless_shell", lambda browsers: Path("shell"))
    monkeypatch.setattr(mermaid, "fetch_bundle", lambda bundle: fetched.append("bundle"))
    monkeypatch.setattr(mermaid, "fetch_browser", lambda browsers: fetched.append("browser"))
    files = mermaid.diagram_files(tmp_path)
    mermaid.fetch_files(files)
    assert fetched == ["bundle"]
    assert files.bundle == tmp_path / "mermaid" / f"mermaid-{mermaid.MERMAID_VERSION}.min.js"
    assert files.browsers == tmp_path / "mermaid" / "browsers"


def test_an_unfetchable_file_sends_code_with_a_notice_and_fetches_once_per_idle_period(tmp_path):
    fetches = []

    async def offline(files):
        fetches.append(files)
        raise DiagramError("mermaid.js could not be fetched: <urlopen error offline>")

    async def scenario():
        c, tx, notices = _client()
        renderer = MermaidRenderer(mermaid.diagram_files(tmp_path), fetch=offline, idle_close=0.3)
        c.adapter.diagrams = renderer
        renderer.prepare()  # the startup fetch
        while renderer._fetch_task is not None:
            await asyncio.sleep(0.01)
        first = await tx.send_rich(WINDOW, _block("graph TD\na-->b"))
        await tx.send_rich(WINDOW, _block("graph TD\nc-->d"))  # within the idle period
        after_two = len(fetches)
        await asyncio.sleep(0.35)
        await tx.send_rich(WINDOW, _block("graph TD\ne-->f"))  # the period is over
        while renderer._fetch_task is not None:
            await asyncio.sleep(0.01)
        return c, first, notices, after_two

    c, first, notices, after_two = asyncio.run(scenario())
    assert '<code class="language-mermaid">' in c.bot.rich[first] and c.bot.rich_media[first] == []
    assert after_two == 1 and len(fetches) == 2
    assert {(n.kind, n.forms, n.error) for n in notices} == {
        (
            NOTICE_FALLBACK,
            "image → code",
            "mermaid.js could not be fetched: <urlopen error offline>",
        )
    }


def _closed_port() -> int:
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]  # closed once the block ends: nothing listens


def _ticking(gaps: list[float]) -> asyncio.Task:
    async def tick():
        last = time.monotonic()
        while True:
            await asyncio.sleep(0.01)
            now = time.monotonic()
            gaps.append(now - last)
            last = now

    return asyncio.create_task(tick())


def test_the_fetch_childs_cause_reaches_the_next_diagram(tmp_path, monkeypatch):
    """The bot's own fetch, offline (every request goes to a closed proxy port): its
    child's cause is kept for the next diagram's notice, and nothing is written."""
    proxy = f"http://127.0.0.1:{_closed_port()}"
    for name in ("https_proxy", "HTTPS_PROXY", "http_proxy", "HTTP_PROXY"):
        monkeypatch.setenv(name, proxy)

    async def scenario():
        renderer = MermaidRenderer(mermaid.diagram_files(tmp_path))
        renderer.prepare()
        while renderer._fetch_task is not None:
            await asyncio.sleep(0.01)
        return renderer._fetch_error

    cause = asyncio.run(asyncio.wait_for(scenario(), 60))
    assert cause.startswith("mermaid.js could not be fetched: ") and "refused" in cause
    assert not (tmp_path / "mermaid" / f"mermaid-{mermaid.MERMAID_VERSION}.min.js").exists()


@pytest.fixture
def silent_proxy(monkeypatch):
    """A proxy that takes the fetch's connection and never answers: the fetch hangs."""
    import socket
    import threading

    silent = socket.socket()
    silent.bind(("127.0.0.1", 0))
    silent.listen()
    held: list[socket.socket] = []
    threading.Thread(target=lambda: held.append(silent.accept()[0]), daemon=True).start()
    for name in ("https_proxy", "HTTPS_PROXY"):
        monkeypatch.setenv(name, f"http://127.0.0.1:{silent.getsockname()[1]}")
    yield held
    for connection in held:
        connection.close()
    silent.close()


def _fetchers(home: Path) -> list[str]:
    """The fetch children running for `home`."""
    found = []
    for cmdline in Path("/proc").glob("[0-9]*/cmdline"):
        try:
            text = cmdline.read_bytes().replace(b"\0", b" ").decode(errors="replace")
        except OSError:
            continue
        if "-m tgforge.base.mermaid" in text and str(home) in text:
            found.append(text)
    return found


def test_startup_fetches_in_a_child_process_without_holding_the_event_loop(tmp_path, silent_proxy):
    async def scenario():
        c = TestClient(home=str(tmp_path))
        c.adapter.diagrams = MermaidRenderer(mermaid.diagram_files(tmp_path))  # the bot's fetch
        gaps: list[float] = []
        ticker = _ticking(gaps)
        await c.core.startup()
        for _ in range(500):  # the child connects to the proxy and hangs there
            if silent_proxy:
                break
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.3)
        running = _fetchers(tmp_path)
        await c.adapter.diagrams.close()
        ticker.cancel()
        c.core._reconcile_task.cancel()
        return running, max(gaps)

    running, longest_hold = asyncio.run(asyncio.wait_for(scenario(), 30))
    assert len(running) == 1 and silent_proxy  # started at startup, in a child process
    assert longest_hold < 0.1, longest_hold


def test_closing_the_renderer_stops_a_running_fetch(tmp_path, silent_proxy):
    async def scenario():
        renderer = MermaidRenderer(mermaid.diagram_files(tmp_path))
        renderer.prepare()
        for _ in range(500):
            if silent_proxy:
                break
            await asyncio.sleep(0.01)
        running = _fetchers(tmp_path)
        started = time.monotonic()
        await renderer.close()
        return running, time.monotonic() - started, _fetchers(tmp_path)

    running, took, after = asyncio.run(asyncio.wait_for(scenario(), 30))
    assert len(running) == 1
    assert took < 2 and after == []


def test_the_test_client_fetches_nothing():
    from tgforge.testing import fetch_nothing

    c = TestClient()
    assert c.adapter.diagrams._fetch_files is fetch_nothing
    with pytest.raises(DiagramError, match="fetches no diagram files"):
        asyncio.run(fetch_nothing(c.adapter.diagrams.files))


def test_the_setup_command_fetches_into_the_bot_home(tmp_path, monkeypatch, capsys):
    from tgforge import cli

    fetched = []
    monkeypatch.setattr(mermaid, "fetch_files", fetched.append)
    cli.main(["fetch-mermaid", "--home", str(tmp_path)])
    assert fetched == [mermaid.diagram_files(tmp_path.resolve())]

    def offline(files):
        raise DiagramError("mermaid.js could not be fetched: offline")

    monkeypatch.setattr(mermaid, "fetch_files", offline)
    cli.main(["fetch-mermaid", "--home", str(tmp_path)])
    assert "mermaid.js could not be fetched: offline — the bot fetches them at startup" in (
        capsys.readouterr().out
    )
