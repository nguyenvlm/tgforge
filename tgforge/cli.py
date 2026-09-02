"""The `tgforge` console-script: a launcher for any tgforge App.

`tgforge run` (no target) starts the default app (every bundled plugin).
`tgforge run module:app` resolves the import string and starts a custom app.
`tgforge install` runs the setup wizard; `tgforge uninstall <name>` tears a bot
down; `tgforge workspaces` edits the agent's workspace roots; `tgforge restart`
restarts a bot's service, detached, from any caller. `run(app)` is the library
entry for a custom __main__.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import os
from pathlib import Path

from tgforge.base.app import App


def _load(target: str) -> App:
    module_path, sep, attr = target.partition(":")
    if not sep or not attr:
        raise SystemExit("target must be 'module:app'")
    app = getattr(importlib.import_module(module_path), attr)
    if not isinstance(app, App):
        raise SystemExit(f"{target} is not a tgforge.App")
    return app


def run(app: App) -> None:
    asyncio.run(app.start())


def _resolve_home(app: str | None, home: str | None) -> Path | None:
    """A real bot home: an explicit --home, else ~/.tgforge/<app>, else the sole
    installed app. Never the ~/.tgforge parent (that would scatter stray state)."""
    from tgforge.install import _list_apps

    if home:
        return Path(home).expanduser().resolve()
    if not app:
        apps = _list_apps()
        if len(apps) == 1:
            app = apps[0]
        elif not apps:
            print("no installed apps — run  tgforge install")
            return None
        else:
            print("which app? pass  --app <name>:")
            for a in apps:
                print(f"  • {a}")
            return None
    return (Path.home() / ".tgforge" / app).resolve()


def _workspaces(action: str, value: str | None, app: str | None, home: str | None) -> None:
    """List/add/remove the agent's workspace roots in a bot home."""
    from tgforge.plugins.claude import config as claude_config

    home_path = _resolve_home(app, home)
    if home_path is None:
        return
    roots = claude_config.read_roots(home_path)
    if action == "add" and value:
        roots.append(value)
        claude_config.write_roots(home_path, roots)
    elif action in ("rm", "remove") and value:
        if value.isdigit() and 1 <= int(value) <= len(roots):
            roots.pop(int(value) - 1)
        elif value in roots:
            roots.remove(value)
        claude_config.write_roots(home_path, roots)
    for i, g in enumerate(roots, 1):
        print(f"  {i}. {g}")
    for w in claude_config.resolve_roots(roots):
        print(f"  • {w}")


def _restart(app: str | None, home: str | None) -> None:
    """Restart a bot's service, source-agnostic: any caller runs the same verb. The
    requester topic (for the post-restart re-entry) is read from env, set only when a
    bot-driven turn runs this — a bare terminal restart just has none."""
    from tgforge.base.config import BotConfig
    from tgforge.base.service import detached_restart

    home_path = _resolve_home(app, home)
    if home_path is None:
        return
    cfg = BotConfig.load(home_path / "bot.json")
    if not cfg.service:
        print("no service configured (set `service` in bot.json)")
        return
    tid = os.environ.get("TELEGRAM_THREAD_ID")
    detached_restart(
        cfg.service, home_path, announce_thread=int(tid) if tid and tid.isdigit() else None
    )
    print(f"restart scheduled for {cfg.service}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="tgforge")
    sub = parser.add_subparsers(dest="command", required=True)
    run_p = sub.add_parser("run", help="launch an app (default app if no target)")
    run_p.add_argument("target", nargs="?", help="import string, e.g. mypkg.main:app")
    sub.add_parser("install", help="interactive setup wizard (token → service → group)")
    un_p = sub.add_parser("uninstall", help="stop + remove a bot (service + home)")
    un_p.add_argument("name", nargs="?", help="the app name; omit to pick from a list")
    un_p.add_argument("--keep-home", action="store_true", help="remove the service, keep state")
    un_p.add_argument("--yes", "-y", action="store_true", help="skip the delete-home prompt")
    ws_p = sub.add_parser("workspaces", help="list/add/remove the agent's workspace roots")
    ws_p.add_argument("action", nargs="?", default="ls", choices=["ls", "add", "rm", "remove"])
    ws_p.add_argument("value", nargs="?", help="a path/glob (add) or index/glob (rm)")
    ws_p.add_argument("--app", help="app name (omit if only one is installed)")
    ws_p.add_argument("--home", help="explicit bot home dir (overrides --app)")
    rs_p = sub.add_parser("restart", help="restart a bot's service (detached)")
    rs_p.add_argument("--app", help="app name (omit if only one is installed)")
    rs_p.add_argument("--home", help="explicit bot home dir (overrides --app)")
    args = parser.parse_args(argv)
    try:
        _dispatch(args)
    except (KeyboardInterrupt, EOFError):
        raise SystemExit("\naborted") from None


def _dispatch(args) -> None:
    if args.command == "run":
        if args.target:
            run(_load(args.target))
        else:
            from tgforge.default import build_default_app

            run(build_default_app())
    elif args.command == "install":
        from tgforge.install import main as install_main

        install_main()
    elif args.command == "uninstall":
        from tgforge.install import uninstall

        uninstall(args.name, keep_home=args.keep_home, assume_yes=args.yes)
    elif args.command == "workspaces":
        _workspaces(args.action, args.value, args.app, args.home)
    elif args.command == "restart":
        _restart(args.app, args.home)
