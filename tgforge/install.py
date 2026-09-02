"""`tgforge install` — an interactive wizard that stands a bot up end to end:
create the bot (BotFather), verify the token, write config, install + start the
user service (systemd on Linux, launchd on macOS) and the claude shell helpers,
then walk the owner through the
Telegram-side steps the Bot API can't do (create a forum group, add the bot,
enable Topics, /init).
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from aiogram import Bot as AioBot


async def _verify_token(token: str) -> str:
    bot = AioBot(token)
    try:
        me = await bot.get_me()
        return me.username
    finally:
        await bot.session.close()


def _ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    ans = input(f"{prompt}{suffix}: ").strip()
    return ans or default


def _select(prompt: str, options: list[str]) -> str | None:
    """Arrow-key picker (↑/↓, Enter; q/Esc cancels). Falls back to a numbered
    prompt when stdin isn't a TTY (or curses is unavailable)."""
    import sys

    if sys.stdin.isatty():
        try:
            import curses

            def _menu(stdscr):
                curses.curs_set(0)
                idx = 0
                while True:
                    stdscr.clear()
                    stdscr.addstr(0, 0, f"{prompt}  (↑/↓, Enter, q to cancel)")
                    for i, opt in enumerate(options):
                        attr = curses.A_REVERSE if i == idx else curses.A_NORMAL
                        stdscr.addstr(i + 2, 2, opt, attr)
                    stdscr.refresh()
                    k = stdscr.getch()
                    if k == curses.KEY_UP:
                        idx = (idx - 1) % len(options)
                    elif k == curses.KEY_DOWN:
                        idx = (idx + 1) % len(options)
                    elif k in (curses.KEY_ENTER, 10, 13):
                        return idx
                    elif k in (27, ord("q")):
                        return None

            picked = curses.wrapper(_menu)
            return options[picked] if picked is not None else None
        except Exception:
            pass
    print(f"{prompt}")
    for i, opt in enumerate(options, 1):
        print(f"  {i}. {opt}")
    sel = _ask("Select (number or name)")
    if sel.isdigit() and 1 <= int(sel) <= len(options):
        return options[int(sel) - 1]
    return sel if sel in options else None


def _wait_for_init(config_path: Path, timeout: int = 300) -> dict | None:
    """Poll bot.json, where the running bot writes owner + chat on /init; return
    them once both are bound, or None on timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            data = json.loads(config_path.read_text())
            if data.get("owner_id") and data.get("chat_id"):
                return data
        except (OSError, json.JSONDecodeError):
            pass
        time.sleep(2)
    return None


def _list_apps() -> list[str]:
    """Installed apps: dirs under ~/.tgforge that hold a bot.json."""
    base = Path.home() / ".tgforge"
    if not base.exists():
        return []
    return sorted(p.name for p in base.iterdir() if p.is_dir() and (p / "bot.json").exists())


def uninstall(name: str | None = None, keep_home: bool = False, assume_yes: bool = False) -> None:
    """Tear a bot down: stop + remove its service and (unless kept) its home
    ~/.tgforge/<name>. With no name, list installed apps and pick one. The
    service name follows the app name."""
    import shutil

    from tgforge.base.service import uninstall_unit

    if not name:
        apps = _list_apps()
        if not apps:
            print("No installed apps found under ~/.tgforge")
            return
        name = _select("Uninstall which app?", apps)
        if not name:
            print("   cancelled")
            return
    name = name.strip().strip("/").split("/")[-1]
    print(f"→ {name}")
    home = Path.home() / ".tgforge" / name
    uninstall_unit(name)
    print(f"   ✓ stopped + removed service {name}")
    if keep_home or not home.exists():
        if keep_home:
            print(f"   home kept: {home}")
        return
    if not assume_yes and _ask(f"Delete home {home}? [y/N]").lower() not in ("y", "yes"):
        print("   home kept")
        return
    shutil.rmtree(home, ignore_errors=True)
    print(f"   ✓ removed home {home}")


def main() -> None:
    print("── tgforge install ──\n")

    name = ""
    while not name:
        name = _ask("App name (its home is ~/.tgforge/<name>)")
        name = name.strip().strip("/").split("/")[-1]  # a bare dir component
    home = Path.home() / ".tgforge" / name
    service = name
    print(f"   → home {home}\n")

    print("Create the bot:")
    print("   • open @BotFather in Telegram → /newbot → follow the prompts")
    print("   • copy the HTTP API token it gives you\n")

    username = ""
    while True:
        token = _ask("Paste the bot token").strip()
        if not token:
            print("   a token is required (or Ctrl-C to abort)")
            continue
        try:
            username = asyncio.run(_verify_token(token))
        except Exception as e:
            print(f"   token rejected by Telegram: {e}\n")
            continue
        print(f"   ✓ token valid — bot is @{username}\n")
        break

    print("Add at least one workspace — a directory the agent can run in.")
    print("(add more later with  tgforge workspaces add <dir>  or /workspaces in Telegram)")
    workspaces: list[str] = []
    while True:
        p = _ask("Workspace directory" + (" (blank to finish)" if workspaces else ""))
        if not p:
            if workspaces:
                break
            print("   at least one is required")
            continue
        path = Path(p).expanduser()
        if not path.is_dir():
            print(f"   not a directory: {path}")
            continue
        resolved = str(path.resolve())
        if resolved in workspaces:
            print("   already added")
            continue
        workspaces.append(resolved)
        print(f"   ✓ {resolved}")

    config_path = home / "bot.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        json.dumps(
            {"token": token, "home": str(home), "service": service},
            indent=2,
        )
        + "\n"
    )
    config_path.chmod(0o600)  # holds the bot token — keep it owner-only
    print(f"\n   ✓ wrote {config_path}")

    from tgforge.base.kernel import IS_MACOS
    from tgforge.base.service import install_unit, service_active
    from tgforge.plugins.claude import config as claude_config
    from tgforge.plugins.claude import setup

    claude_config.write_roots(home, workspaces)
    print(f"   ✓ configured {len(workspaces)} workspace root(s)")
    setup.install_claude_as()
    setup.install_shell_wrapper()
    print("   ✓ installed the claude shell helpers")
    unit = install_unit(service, home)
    print(f"   ✓ installed {unit}")
    logs = (
        "log show --predicate 'process == \"tgforge\"' --last 5m"
        if IS_MACOS
        else f"journalctl --user -u {service} -e"
    )
    if service_active(service):
        print("   ✓ service is running\n")
    else:
        print(f"   ⚠ service not active — check:  {logs}\n")

    print("Finish in Telegram (the Bot API can't do these for you) —")
    print("I'll wait after each step.\n")
    input(
        "  1) Create a new group, then make it a forum: Group → Edit → Topics ON.\n"
        "     Press Enter when done… "
    )
    input(
        f"  2) Add @{username} to the group and promote it to admin.\n     Press Enter when done… "
    )
    print(f"  3) In the group's General topic, send:  @{username} /init")
    print("     (binds you as owner + this group as the bot's chat)")
    print("     Waiting for the bot to receive it… (Ctrl-C to skip)")

    try:
        bound = _wait_for_init(config_path)
    except KeyboardInterrupt:
        bound = None
    status_cmd = (
        f"launchctl print gui/$(id -u)/{service}"
        if IS_MACOS
        else f"systemctl --user status {service}"
    )
    if bound:
        print(f"\n   ✓ verified — owner {bound['owner_id']}, chat {bound['chat_id']}")
        print(f"   open a session:  @{username} /claude <name>\n")
    else:
        print(f"\n   ⚠ no /init seen yet. The bot is running — send  @{username} /init")
        print(f"     anytime, then check:  {status_cmd}\n")
    print("Manage the service:")
    print(f"   {status_cmd}")
    print(f"   {logs}")
