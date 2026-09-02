# tgforge

Run agents and tools from Telegram. tgforge turns a Telegram forum group into a desktop: each topic is a window — a Claude Code session, an interactive shell, a file browser — and the bot routes your messages to whichever window you're typing in.

There are two ways to use it: run the **out-of-the-box app** (all bundled plugins, no code), or **define your own app** in a few lines of Python.

## 1. Out of the box

```
uv pip install "tgforge @ git+https://github.com/nguyenvlm/tgforge"
tgforge install     # wizard: name → token → config → user service → guided group setup
```

The wizard installs the bot as a user service (systemd on Linux, launchd on macOS) and walks you through connecting it to a forum group. That's it — the default app ships every bundled plugin:

- **Claude** (`/claude`) — a Claude Code session per window, with model/account/workspace pickers, plus `!` / `!!` one-shot shell prefixes anywhere.
- **Shell** (`/shell`) — a persistent PTY: each message is a line to stdin (slashes included), with Ctrl-C / Ctrl-D / close buttons. Survives a bot restart and revives in the last cwd.
- **Files** (`/localfs`) — a tap-to-navigate file browser with `/bookmarks`.
- **gcloud** (`/gcloud`) — browserless Google Cloud sign-in.

Built-in commands work in every window: `/` opens the button menu, `/new`, `/close`, `/help`, `/usage`, `/restart`. To run it in the foreground instead of the service: `tgforge run`. To remove a bot: `tgforge uninstall [name]` (`--keep-home` keeps its state).

Only the bound owner can use the bot (you bind yourself with `/init` during setup).

## 2. Your own app

Pick plugins, configure them, run it:

```python
# mybot.py
from tgforge import App
from tgforge.plugins.claude import Claude
from tgforge.plugins.shell import Shell

app = App(config="bot.json")
app.include(Claude(model="claude-opus-4-8[1m]"))
app.include(Shell())
```

```
tgforge run mybot:app
```

Or from your own `__main__`: `tgforge.run(app)`.

Writing a plugin: subclass `Topic` (one window class), declare commands with decorators (`@launch`, `@command`, `@universal`, `@on_message`, `@action`), and list the class in a `Plugin`. A window keeps live state on the instance and persistent state in `self.saved` — a dict-like view over the bot's SQLite file; a plugin never opens a file and never receives the kernel. `example/mybot.py` and `example/allowlist.py` (a custom auth middleware with an admin `/allow` command) are worked examples.

## Configuration

`bot.json` (in the bot home, gitignored — it carries the token):

```json
{
  "token": "<bot-token>",
  "home": "~/.tgforge/mybot",
  "service": "mybot"
}
```

`home` is the bot's state dir and process cwd; the wizard puts each bot under `~/.tgforge/<name>`. `service` names the user service, which enables `/restart`.

Claude-plugin extras:

- **Workspaces** (where the agent runs): `tgforge workspaces add <path-or-glob>` on the CLI, or `/workspaces` in Telegram. Switching a topic to another root starts a fresh session there; the old one stays resumable.
- **Per-repo guidance**: pass `Claude(brief="…")` or drop a `session_brief.md` in the bot home — appended to every agent turn.
- Every turn is tagged `[via Telegram bot · <bot> · …]` so the agent knows where it's running.

## How it works

`tgforge/base/` is five modules: `kernel.py` (the whole core — persistence, the plugin API, Telegram transport, command registry, router, window lifecycle, menus), `app.py` (the `App` + `OwnerOnly` auth), `ui.py`, `config.py`, `service.py` (the managed service + detached restart).

Each forum topic is a window owned by one class. The kernel routes an event down a chain — the window's own commands, then universal commands, then the window's `on_message` / `on_unknown` — and each handler may consume it or return `False` to fall through. General (outside any topic) reacts only to an `@mention`. Window titles track state (`Claude · <session>`, `Shell · <cwd>`) via a `title_suffix()` hook.
