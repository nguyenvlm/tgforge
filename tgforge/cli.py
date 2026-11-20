"""The `tgforge` console-script: a launcher for any tgforge App.

`tgforge run` (no target) starts the default app (every bundled plugin).
`tgforge run module:app` resolves the import string and starts a custom app.
`tgforge install` runs the setup wizard; `tgforge uninstall <name>` tears a bot
down; `tgforge workspaces` edits the agent's workspace roots; `tgforge model`,
`tgforge mode` and `tgforge window` change an agent plugin's model list, permission
mode and a window's model / effort (the running bot reads them at each use);
`tgforge restart` restarts a bot's service, detached, from any caller. `run(app)` is
the library entry for a custom __main__.
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
    from tgforge.plugins.agent.window import read_roots, resolve_roots, write_roots

    home_path = _resolve_home(app, home)
    if home_path is None:
        return
    roots = read_roots(home_path)
    if action == "add" and value:
        roots.append(value)
        write_roots(home_path, roots)
    elif action in ("rm", "remove") and value:
        if value.isdigit() and 1 <= int(value) <= len(roots):
            roots.pop(int(value) - 1)
        elif value in roots:
            roots.remove(value)
        write_roots(home_path, roots)
    for i, g in enumerate(roots, 1):
        print(f"  {i}. {g}")
    for w in resolve_roots(roots):
        print(f"  • {w}")


def _agent_plugin(plugin_id: str, home: Path):
    """An agent plugin bound to a bot home's stores, as the running bot binds it."""
    from tgforge.base.kernel import AppDB, bind_store

    if plugin_id == "agy":
        from tgforge.plugins.agy import Agy as plugin_class
    else:
        from tgforge.plugins.claude import Claude as plugin_class
    plugin = plugin_class()
    bind_store(plugin, AppDB(home / "state.db"))
    return plugin


def _plugin_id(plugin_id: str | None) -> str:
    """The agent plugin a command is for: `--plugin`, else the window's own
    (`TGFORGE_PLUGIN`, set in an agent window's CLI), else `claude`."""
    return plugin_id or os.environ.get("TGFORGE_PLUGIN") or "claude"


def _pool_model(plugin, plugin_id: str, model: str) -> str:
    """The model value a label or value names in the plugin's model list; refused
    when the list has no such model."""
    pool = plugin.model_pool()
    match = next((v for label, v in pool if model in (label, v)), None)
    if match is None:
        offered = ", ".join(v for _, v in pool) or "none — add one with  tgforge model add"
        raise SystemExit(f"model '{model}' is not in the {plugin_id} model list: {offered}")
    return match


def _model(action: str, value: str | None, plugin_id: str, home: Path) -> None:
    """List or edit an agent plugin's model list, or set its default model."""
    plugin = _agent_plugin(plugin_id, home)
    if action in ("add", "rm", "default") and not value:
        raise SystemExit(f"tgforge model {action} needs a value")
    if action == "default":
        value = _pool_model(plugin, plugin_id, value)
        plugin.set_default_model(value)
        print(f"default model → {value}")
    elif action != "list":
        print(plugin.edit_models(f"{action} {value or ''}"))
    print(plugin.models_text())


def _mode(value: str | None, plugin_id: str, home: Path) -> None:
    """Print an agent plugin's permission mode, or set it to one of its modes."""
    plugin = _agent_plugin(plugin_id, home)
    if value is not None:
        modes = plugin.topics[0].PERMISSION_MODES
        if value not in modes:
            raise SystemExit(f"unknown mode '{value}' — one of: {', '.join(modes)}")
        plugin.set_permission_mode(value)
        print(f"{plugin_id} mode → {value} (each window's next process runs it)")
        return
    print(f"{plugin_id} mode: {plugin.permission_mode}")


def _window(
    model: str | None,
    effort: str | None,
    plugin_id: str,
    window: str | None,
    home: Path,
) -> None:
    """Print or set the model and effort of the agent window the command runs in
    (`TGFORGE_PLUGIN` / `TGFORGE_WINDOW`); the window applies a change before its next
    turn. Every value is checked before any is written."""
    from tgforge.base.kernel import AppDB, window_ns
    from tgforge.plugins.agent.window import write_window_setting

    window = window or os.environ.get("TGFORGE_WINDOW")
    if not window:
        raise SystemExit("no window — run this inside an agent window or pass --window <key>")
    plugin = _agent_plugin(plugin_id, home)
    topic_class = plugin.topics[0]
    store = AppDB(home / "state.db").saved(window_ns(topic_class.id, window))
    if "session" not in store:
        raise SystemExit(f"no {plugin_id} window '{window}'")
    wanted: dict[str, str | None] = {}
    if model is not None:
        wanted["model"] = None if model == "default" else _pool_model(plugin, plugin_id, model)
    if effort is not None:
        if effort != "default" and effort not in topic_class.EFFORTS:
            levels = ", ".join((*topic_class.EFFORTS, "default"))
            raise SystemExit(f"effort '{effort}' is not one of: {levels}")
        wanted["effort"] = None if effort == "default" else effort
    for name, value in wanted.items():
        write_window_setting(store, name, value)
    print(f"model {store.get('model') or 'default'} · effort {store.get('effort') or 'default'}")


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
    window = os.environ.get("TGFORGE_WINDOW") or None
    detached_restart(cfg.service, home_path, announce_window=window)
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
    agent_help = "agent plugin (claude or agy)"
    md_p = sub.add_parser("model", help="list/edit an agent plugin's model list")
    md_p.add_argument(
        "action", nargs="?", default="list", choices=["list", "add", "rm", "reset", "default"]
    )
    md_p.add_argument(
        "value", nargs="?", help="label|value (add), n or value (rm), value (default)"
    )
    mo_p = sub.add_parser("mode", help="show or set an agent plugin's permission mode")
    mo_p.add_argument("value", nargs="?", help="the mode to set")
    wn_p = sub.add_parser("window", help="show or set this agent window's model / effort")
    wn_p.add_argument("--model", help="a model from the plugin's list, or default")
    wn_p.add_argument("--effort", help="an /effort level, or default")
    wn_p.add_argument("--window", help="the window key (default: $TGFORGE_WINDOW)")
    for p in (md_p, mo_p, wn_p):
        p.add_argument("--plugin", choices=["claude", "agy"], help=agent_help)
        p.add_argument("--app", help="app name (omit if only one is installed)")
        p.add_argument("--home", help="explicit bot home dir (overrides --app)")
    fm_p = sub.add_parser(
        "fetch-mermaid", help="fetch mermaid.js and the headless browser into a bot home"
    )
    fm_p.add_argument("--app", help="app name (omit if only one is installed)")
    fm_p.add_argument("--home", help="explicit bot home dir (overrides --app)")
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
    elif args.command in ("model", "mode", "window"):
        home_path = _resolve_home(args.app, args.home)
        if home_path is None:
            return
        if args.command == "model":
            _model(args.action, args.value, _plugin_id(args.plugin), home_path)
        elif args.command == "mode":
            _mode(args.value, _plugin_id(args.plugin), home_path)
        else:
            _window(args.model, args.effort, _plugin_id(args.plugin), args.window, home_path)
    elif args.command == "restart":
        _restart(args.app, args.home)
    elif args.command == "fetch-mermaid":
        from tgforge.install import fetch_diagram_files

        home_path = _resolve_home(args.app, args.home)
        if home_path is not None:
            fetch_diagram_files(home_path)
