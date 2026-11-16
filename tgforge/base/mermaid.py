"""Mermaid source to PNG: the mermaid.js bundle in Playwright's headless Chromium. Both
are fetched into the bot home (`diagram_files`): the bundle pinned by version and sha256
(a file whose sha256 differs is deleted, never loaded), the browser the revision the
installed Playwright expects. One browser page is kept open across diagrams and closed
after `IDLE_CLOSE` seconds without one; Playwright drives the browser from its own
process and the bot fetches in a child process (`fetch_in_subprocess`, killed when the
renderer closes), so a diagram never holds the event loop."""

from __future__ import annotations

import asyncio
import functools
import hashlib
import json
import logging
import os
import signal
import subprocess
import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import playwright
from playwright.async_api import Error, async_playwright

LOGGER = logging.getLogger("tgforge")

MERMAID_VERSION = "12.3.0"
MERMAID_URL = f"https://cdn.jsdelivr.net/npm/mermaid@{MERMAID_VERSION}/dist/mermaid.min.js"
MERMAID_SHA256 = "336df803d87a38b70a01e736e666c8f7601d2ce42f5e47c8fafe5d721074b9a3"
BUNDLE_FETCH_TIMEOUT = 120.0  # seconds for the bundle's download
BROWSER_FETCH_TIMEOUT = 900.0  # seconds for the browser's download and unpack
IDLE_CLOSE = 300.0  # seconds without a diagram before the browser closes
DRAW_TIMEOUT = 20.0  # seconds one diagram may take, the browser's start left out
SCALE = 2  # device pixels per CSS pixel in the PNG
# Telegram's photo limits: width + height, the longer side over the shorter, the bytes
PHOTO_SIDES_MAX = 10000
PHOTO_RATIO_MAX = 20
PHOTO_BYTES_MAX = 10 * 1024 * 1024

_PAGE = (
    "<!doctype html><html><head><meta charset='utf-8'></head>"
    "<body style='margin:0;background:white'>"
    "<div id='out' style='display:inline-flex;align-items:center;justify-content:center;"
    "background:white'></div></body></html>"
)
# draws one diagram into #out: its box padded so neither side passes `ratio` times the
# other, then shrunk so the padded sides stay within `sides` CSS pixels in sum;
# {error} when Mermaid refuses the source
_DRAW = """async ([id, code, sides, ratio]) => {
  const out = document.getElementById('out');
  out.innerHTML = '';
  let svg;
  try {
    ({ svg } = await mermaid.render(id, code));
  } catch (error) {
    document.getElementById('d' + id)?.remove();
    return { error: String((error && error.message) || error) };
  }
  out.innerHTML = svg;
  const element = out.querySelector('svg');
  element.style.maxWidth = 'none';
  const box = element.viewBox.baseVal;
  const padded = [Math.max(box.width, box.height / ratio), Math.max(box.height, box.width / ratio)];
  const shrink = Math.min(1, sides / (padded[0] + padded[1]));
  element.setAttribute('width', box.width * shrink);
  element.setAttribute('height', box.height * shrink);
  out.style.width = Math.ceil(padded[0] * shrink) + 'px';
  out.style.height = Math.ceil(padded[1] * shrink) + 'px';
  return {};
}"""


class DiagramError(Exception):
    """A diagram that did not draw, or a diagram file that did not fetch; its text names
    the cause."""


@dataclass
class DiagramFiles:
    """Where a bot's diagram files live: the mermaid.js bundle and Playwright's
    browsers directory."""

    bundle: Path
    browsers: Path


def diagram_files(home: Path) -> DiagramFiles:
    """The diagram files of the bot whose home is `home`."""
    root = Path(home) / "mermaid"
    return DiagramFiles(root / f"mermaid-{MERMAID_VERSION}.min.js", root / "browsers")


def bundle_ok(bundle: Path) -> bool:
    """Whether the bundle is there with the pinned sha256; one that differs is deleted."""
    if not bundle.is_file():
        return False
    digest = hashlib.sha256(bundle.read_bytes()).hexdigest()
    if digest == MERMAID_SHA256:
        return True
    LOGGER.warning("deleted %s: sha256 %s, not the pinned %s", bundle, digest, MERMAID_SHA256)
    bundle.unlink()
    return False


def fetch_bundle(bundle: Path, url: str = MERMAID_URL) -> None:
    """Download the bundle to `bundle`; one whose sha256 differs is never written."""
    try:
        with urllib.request.urlopen(url, timeout=BUNDLE_FETCH_TIMEOUT) as response:
            data = response.read()
    except OSError as error:
        raise DiagramError(f"mermaid.js could not be fetched: {error}") from error
    digest = hashlib.sha256(data).hexdigest()
    if digest != MERMAID_SHA256:
        raise DiagramError(
            f"mermaid.js from {url} has sha256 {digest}, not the pinned {MERMAID_SHA256}"
        )
    bundle.parent.mkdir(parents=True, exist_ok=True)
    part = bundle.with_name(f"{bundle.name}.{os.getpid()}.part")
    part.write_bytes(data)
    part.replace(bundle)


@functools.cache
def _shell_revision() -> str:
    """The headless browser revision the installed Playwright expects."""
    listing = Path(playwright.__file__).parent / "driver" / "package" / "browsers.json"
    return next(
        entry["revision"]
        for entry in json.loads(listing.read_text())["browsers"]
        if entry["name"] == "chromium-headless-shell"
    )


def headless_shell(browsers: Path) -> Path | None:
    """The headless browser in `browsers` at the revision the installed Playwright
    expects; None when it is not there."""
    for executable in sorted(browsers.glob(f"chromium_headless_shell-{_shell_revision()}/*/*")):
        if executable.name.startswith("chrome-headless-shell") and executable.is_file():
            return executable
    return None


def fetch_browser(browsers: Path) -> None:
    """Install Playwright's headless browser into `browsers`."""
    command = [sys.executable, "-m", "playwright", "install", "chromium-headless-shell"]
    environment = {**os.environ, "PLAYWRIGHT_BROWSERS_PATH": str(browsers)}
    try:
        run = subprocess.run(
            command,
            env=environment,
            capture_output=True,
            text=True,
            timeout=BROWSER_FETCH_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise DiagramError(f"the headless browser could not be fetched: {error}") from error
    if run.returncode != 0 or headless_shell(browsers) is None:
        output = (run.stderr or run.stdout).strip().splitlines()
        cause = output[-1][:200] if output else f"exit {run.returncode}"
        raise DiagramError(f"the headless browser could not be fetched: {cause}")


def files_ready(files: DiagramFiles) -> bool:
    """Whether the bundle (with its pinned sha256) and the browser are both there."""
    return bundle_ok(files.bundle) and headless_shell(files.browsers) is not None


def fetch_files(files: DiagramFiles) -> None:
    """Fetch whichever of the bundle and the browser is missing (blocking: tgforge's setup
    calls it directly, the bot through `fetch_in_subprocess`)."""
    if not bundle_ok(files.bundle):
        fetch_bundle(files.bundle)
    if headless_shell(files.browsers) is None:
        fetch_browser(files.browsers)


async def fetch_in_subprocess(files: DiagramFiles) -> None:
    """`fetch_files` in a child process of its own session (`python -m
    tgforge.base.mermaid`); its failure raised as a `DiagramError` with the child's
    cause; a cancel kills the child and everything it started."""
    child = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "tgforge.base.mermaid",
        str(files.bundle),
        str(files.browsers),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        _output, errors = await child.communicate()
    except asyncio.CancelledError:
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await child.wait()
        raise
    if child.returncode != 0:
        lines = errors.decode(errors="replace").strip().splitlines()
        raise DiagramError(lines[-1] if lines else f"the fetch exited with {child.returncode}")


class BrowserPage:
    """One headless Chromium page with mermaid.js loaded."""

    def __init__(self, playwright, browser, page):
        self.playwright = playwright
        self.browser = browser
        self.page = page

    async def draw(self, source: str, serial: int) -> bytes:
        """`source` as a PNG within Telegram's photo limits."""
        sides = PHOTO_SIDES_MAX / SCALE - 16  # a few pixels of rounding room
        result = await self.page.evaluate(
            _DRAW, [f"diagram{serial}", source, sides, PHOTO_RATIO_MAX - 0.5]
        )
        if "error" in result:
            raise DiagramError(f"Mermaid could not draw it: {result['error']}"[:300])
        png = await self.page.locator("#out").screenshot(type="png")
        if len(png) > PHOTO_BYTES_MAX:
            raise DiagramError(f"its PNG is {len(png)} bytes, over a photo's {PHOTO_BYTES_MAX}")
        return png

    async def close(self) -> None:
        try:
            await self.browser.close()
        finally:
            await self.playwright.stop()


async def open_browser_page(files: DiagramFiles) -> BrowserPage:
    """Start Playwright and the fetched headless browser on a page with the fetched
    mermaid.js loaded."""
    executable = headless_shell(files.browsers)
    if executable is None:
        raise DiagramError(f"the headless browser is missing from {files.browsers}")
    driver = await async_playwright().start()
    try:
        browser = await driver.chromium.launch(executable_path=str(executable))
    except Error as error:
        await driver.stop()
        first_line = str(error).strip().splitlines()[0][:200]
        raise DiagramError(f"the headless browser did not start: {first_line}") from error
    try:
        page = await browser.new_page(device_scale_factor=SCALE)
        await page.route("**/*", lambda route: route.abort())  # a diagram fetches nothing
        await page.set_content(_PAGE)
        await page.add_script_tag(path=str(files.bundle))
        await page.evaluate("mermaid.initialize({ startOnLoad: false })")
    except Error as error:
        await BrowserPage(driver, browser, None).close()
        raise DiagramError(f"mermaid.js did not load: {error}"[:300]) from error
    return BrowserPage(driver, browser, page)


class MermaidRenderer:
    """Draws Mermaid diagrams one at a time on one browser page, opened on the first
    diagram and closed `idle_close` seconds after the last. The files (`files`, None
    when the bot has no home) are fetched by `prepare` at startup, and again by a
    diagram that finds them missing at most once per `idle_close`; the diagram itself
    goes out as code meanwhile. `open_page` and `fetch` are `open_browser_page` and
    `fetch_in_subprocess`, stand-ins in tests."""

    def __init__(
        self,
        files: DiagramFiles | None,
        open_page=open_browser_page,
        fetch=fetch_in_subprocess,
        idle_close=IDLE_CLOSE,
        timeout=DRAW_TIMEOUT,
    ):
        self.files = files
        self._open_page = open_page
        self._fetch_files = fetch
        self.idle_close = idle_close
        self.timeout = timeout
        self._page = None
        self._lock = asyncio.Lock()
        self._idle_timer: asyncio.TimerHandle | None = None
        self._idle_task: asyncio.Task | None = None
        self._serial = 0  # diagrams drawn: an idle close skips when one drew since it fired
        self._fetch_task: asyncio.Task | None = None
        self._fetch_error = ""  # why the last fetch failed
        self._next_fetch = 0.0  # loop time before which a diagram starts no fetch

    @property
    def is_open(self) -> bool:
        return self._page is not None

    def prepare(self) -> None:
        """Fetch whichever diagram file is missing, in a child process (at startup)."""
        self._start_fetch()

    def _start_fetch(self) -> None:
        if self.files is None or self._fetch_task is not None:
            return
        loop = asyncio.get_running_loop()
        self._next_fetch = loop.time() + self.idle_close
        self._fetch_task = loop.create_task(self._fetch())

    async def _fetch(self) -> None:
        try:
            await self._fetch_files(self.files)
            self._fetch_error = ""
        except DiagramError as error:
            self._fetch_error = str(error)
            LOGGER.warning("diagram files not fetched: %s", error)
        finally:
            self._fetch_task = None

    async def _ready_page(self):
        """The open page, else a new one on files that check out; a missing or changed
        file raises its cause and starts a fetch (at most once per idle period)."""
        if self._page is not None:
            return self._page
        if self.files is None:
            raise DiagramError("the bot has no home to fetch the diagram files into")
        if self._fetch_task is not None:
            raise DiagramError("mermaid.js and the headless browser are being fetched")
        if await asyncio.to_thread(files_ready, self.files):
            self._page = await self._open_page(self.files)
            return self._page
        if asyncio.get_running_loop().time() >= self._next_fetch:
            self._start_fetch()
        raise DiagramError(self._fetch_error or "mermaid.js or the headless browser is missing")

    async def draw(self, source: str) -> bytes:
        """`source` as a PNG; raises `DiagramError` naming why it did not draw (a source
        Mermaid refuses, a file missing or unfetchable, a timeout). A timeout or a browser
        failure closes the browser, so the next diagram starts a fresh one."""
        async with self._lock:
            self._cancel_idle()
            try:
                page = await self._ready_page()
                self._serial += 1
                return await asyncio.wait_for(page.draw(source, self._serial), self.timeout)
            except DiagramError:
                raise
            except TimeoutError as error:
                await self._close_page()
                raise DiagramError(f"timed out after {self.timeout:.0f} s") from error
            except Exception as error:
                await self._close_page()
                raise DiagramError(f"the headless browser failed: {error}"[:300]) from error
            finally:
                self._arm_idle()

    async def close(self) -> None:
        """Close the browser and stop a running fetch (at shutdown)."""
        fetching = self._fetch_task
        if fetching is not None:
            fetching.cancel()
            await asyncio.gather(fetching, return_exceptions=True)
        async with self._lock:
            self._cancel_idle()
            await self._close_page()

    async def _close_idle(self, serial: int) -> None:
        """The idle close: skipped when a diagram drew while it waited for the lock (that
        draw armed a fresh idle timer)."""
        async with self._lock:
            if self._serial == serial:
                self._cancel_idle()
                await self._close_page()

    def _arm_idle(self) -> None:
        if self._page is not None:
            loop = asyncio.get_running_loop()
            self._idle_timer = loop.call_later(self.idle_close, self._idle_expired)

    def _cancel_idle(self) -> None:
        if self._idle_timer is not None:
            self._idle_timer.cancel()
            self._idle_timer = None

    def _idle_expired(self) -> None:
        self._idle_timer = None
        self._idle_task = asyncio.get_running_loop().create_task(self._close_idle(self._serial))

    async def _close_page(self) -> None:
        page, self._page = self._page, None
        if page is None:
            return
        try:
            await page.close()
        except Exception:
            LOGGER.warning("the diagram browser did not close cleanly", exc_info=True)


if __name__ == "__main__":  # `fetch_in_subprocess`: python -m tgforge.base.mermaid BUNDLE BROWSERS
    try:
        fetch_files(DiagramFiles(Path(sys.argv[1]), Path(sys.argv[2])))
    except DiagramError as error:
        print(error, file=sys.stderr)
        raise SystemExit(1) from error
