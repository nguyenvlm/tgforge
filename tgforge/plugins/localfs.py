"""Local-filesystem browser: `/localfs` opens a tap-to-navigate window over the
whole filesystem. Each tap edits the window's panel in place; the current dir /
file view persists in the window store, so a restart re-renders where it left off.
"""

from __future__ import annotations

import os
from pathlib import Path

from tgforge.base import ui
from tgforge.base.kernel import Plugin, Topic, action, command, launch
from tgforge.base.ui import MAX_MSG

PAGE = 10  # dir entries per page
PREVIEW_BYTES = 3000
PREVIEW_LINES = 40


def _human_size(n: int) -> str:
    size = float(n)
    for unit in ("B", "K", "M", "G", "T"):
        if size < 1024:
            return f"{size:.0f}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}P"


def _code(content: str) -> str:
    budget = MAX_MSG - 10
    esc = content.replace("\\", "\\\\").replace("`", "\\`")
    if len(esc) > budget:
        content = content[: budget // 2]
        esc = content.replace("\\", "\\\\").replace("`", "\\`")
    return f"```\n{esc}\n```"


@launch("/localfs", "browse the local filesystem")
class LocalfsTopic(Topic):
    id = "localfs"
    icon = "📁"
    menu_label = "Files"

    def __init__(self, core, thread_id, name, saved):
        super().__init__(core, thread_id, name, saved)
        self.msg_id: int | None = None

    def title_suffix(self) -> str | None:
        cwd = self.saved.get("cwd")
        return (Path(cwd).name or "/") if cwd else None

    async def _pick_start(self) -> str:
        home = str(self._core.config.home_path)
        bms = self.plugin.bookmarks
        if not bms:
            return home
        opts = [("🏠 Home", home)] + [(Path(b).name or b, b) for b in bms]
        return await self.menu("📂 Start in", opts) or home

    async def on_open(self):
        self.saved["cwd"] = await self._pick_start()
        self.saved["view"] = None
        self.saved["page"] = 0
        self.msg_id = await self.send("opening…")
        await self._redraw()
        await self.refresh_title()

    async def on_revive(self):
        self.msg_id = await self.send("↻ browser restored")
        await self._redraw()
        await self.refresh_title()

    # ── Listing + views ────────────────────────────────────────────
    def _list(self, cwd: str):
        try:
            with os.scandir(cwd) as it:
                raw = list(it)
        except OSError as e:
            return None, str(e)
        entries = []
        for e in raw:
            try:
                is_dir = e.is_dir(follow_symlinks=True)
            except OSError:
                is_dir = False
            size = 0
            if not is_dir:
                try:
                    size = e.stat(follow_symlinks=True).st_size
                except OSError:
                    size = 0
            entries.append({"name": e.name, "is_dir": is_dir, "path": e.path, "size": size})
        entries.sort(key=lambda x: (not x["is_dir"], x["name"].lower()))
        return entries, None

    def _dir_view(self):
        cwd = self.saved["cwd"]
        entries, err = self._list(cwd)
        rows: list = []
        if err is not None:
            text = f"📂 {cwd}\n\n⚠ {err}"
        else:
            total = len(entries)
            pages = max(1, (total + PAGE - 1) // PAGE)
            page = max(0, min(self.saved.get("page", 0), pages - 1))
            self.saved["page"] = page
            lo = page * PAGE
            text = f"📂 {cwd}\n{total} item(s) · page {page + 1}/{pages}"
            for i, e in enumerate(entries[lo : lo + PAGE]):
                gi = lo + i
                if e["is_dir"]:
                    rows.append([ui.act(f"📁 {e['name'][:34]}", "fs", f"cd:{gi}")])
                else:
                    label = f"📄 {e['name'][:30]} · {_human_size(e['size'])}"
                    rows.append([ui.act(label, "fs", f"file:{gi}")])
            nav = []
            if page > 0:
                nav.append(ui.act("‹ prev", "fs", f"pg:{page - 1}"))
            if page < pages - 1:
                nav.append(ui.act("next ›", "fs", f"pg:{page + 1}"))
            if nav:
                rows.append(nav)
        bottom = []
        if cwd != "/":
            bottom.append(ui.act("⬆ ..", "fs", "up"))
        bottom.append(ui.act("⭐ marks", "fs", "bm"))
        bottom.append(ui.act("❌ close", "fs", "close"))
        rows.append(bottom)
        return text, ui.keyboard(rows)

    def _file_view(self):
        path = self.saved["view"]
        back = ui.act("🔙 back", "fs", "back")
        close = ui.act("❌ close", "fs", "close")
        try:
            size = os.path.getsize(path)
        except OSError as e:
            content = f"⚠ {e}"
            return _code(content), content, ui.keyboard([[back, close]])
        head = f"📄 {path}\n{_human_size(size)}"
        try:
            with open(path, "rb") as f:
                raw = f.read(PREVIEW_BYTES)
            if b"\x00" in raw:
                preview = "\n\n(binary file — download to view)"
            else:
                lines = raw.decode("utf-8", "replace").splitlines()
                body = "\n".join(lines[:PREVIEW_LINES])
                more = "\n…" if len(lines) > PREVIEW_LINES or size > PREVIEW_BYTES else ""
                preview = f"\n\n{body}{more}"
        except OSError as e:
            preview = f"\n\n⚠ {e}"
        content = head + preview
        dl = ui.act("⬇ download", "fs", "dl")
        return _code(content), content, ui.keyboard([[dl], [back, close]])

    async def _redraw(self):
        if self.msg_id is None:
            return
        if self.saved.get("view"):
            md, plain, markup = self._file_view()
            await self.edit_md(self.msg_id, md, plain, reply_markup=markup)
        else:
            text, markup = self._dir_view()
            await self.edit(self.msg_id, text, reply_markup=markup)

    # ── Button routing ─────────────────────────────────────────────
    @action("fs")
    async def button(self, ctx, arg):
        parts = arg.split(":", 1)
        act, idx = parts[0], (parts[1] if len(parts) > 1 else "")
        if act == "close":
            await self.close()
            return
        if act == "bm":
            await self._bookmarks_menu()
            return
        if act == "dl":
            try:
                await self.send_file(Path(self.saved["view"]))
            except (RuntimeError, OSError) as e:
                await self.send(f"download failed: {e}")
            return
        if act in ("cd", "file"):
            entries, err = self._list(self.saved["cwd"])
            if err is None and idx.isdigit() and int(idx) < len(entries):
                e = entries[int(idx)]
                if act == "cd":
                    self.saved["cwd"] = e["path"]
                    self.saved["page"] = 0
                    self.saved["view"] = None
                else:
                    self.saved["view"] = e["path"]
        elif act == "up":
            self.saved["cwd"] = os.path.dirname(self.saved["cwd"]) or "/"
            self.saved["page"] = 0
            self.saved["view"] = None
        elif act == "pg" and idx.isdigit():
            self.saved["page"] = int(idx)
        elif act == "back":
            self.saved["view"] = None
        await self._redraw()
        if act in ("cd", "up"):
            await self.refresh_title()

    # ── Bookmarks ──────────────────────────────────────────────────
    @command("/bookmarks", "go to or edit bookmarked folders", icon="⭐")
    async def bookmarks_cmd(self, ctx):
        await self._bookmarks_menu()

    async def _bookmarks_menu(self):
        bms = self.plugin.bookmarks
        opts = []
        if bms:
            opts.append(("📂 Go to a bookmark", "go"))
        opts.append(("⭐ Bookmark this folder", "add"))
        if bms:
            opts.append(("➖ Remove a bookmark", "remove"))
        choice = await self.menu("⭐ Bookmarks", opts)
        if choice == "go":
            pick = await self.menu("📂 Go to", [(Path(b).name or b, b) for b in bms])
            if pick:
                self.saved["cwd"] = pick
                self.saved["page"] = 0
                self.saved["view"] = None
                await self._redraw()
                await self.refresh_title()
        elif choice == "add":
            n = self.plugin.add_bookmark(self.saved["cwd"])
            await self.send(f"⭐ bookmarked {Path(self.saved['cwd']).name} — {n} bookmark(s)")
        elif choice == "remove":
            pick = await self.menu(
                "➖ Remove", [(Path(b).name or b, str(i)) for i, b in enumerate(bms)]
            )
            if pick is not None:
                removed = self.plugin.remove_bookmark(int(pick))
                if removed:
                    await self.send(f"removed bookmark '{removed}'")


class Localfs(Plugin):
    id = "localfs"
    topics = [LocalfsTopic]

    def __init__(self, bookmarks=None):
        self._init_bookmarks = list(bookmarks) if bookmarks else []

    @property
    def bookmarks(self) -> list[str]:
        """Saved folder paths (plugin-store edits win over the seeded defaults)."""
        stored = self.saved.get("bookmarks") if hasattr(self, "saved") else None
        return list(stored) if stored is not None else list(self._init_bookmarks)

    def add_bookmark(self, path: str) -> int:
        bms = self.bookmarks
        if path not in bms:
            bms.append(path)
            self.saved["bookmarks"] = bms
        return len(bms)

    def remove_bookmark(self, idx: int) -> str | None:
        bms = self.bookmarks
        if 0 <= idx < len(bms):
            removed = bms.pop(idx)
            self.saved["bookmarks"] = bms
            return removed
        return None
