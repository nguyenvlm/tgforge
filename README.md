# tgforge

Run agents and tools from Telegram or Slack. tgforge turns a Telegram forum group — or a Slack workspace — into a desktop: each topic (Slack: each private channel) is a window — a Claude Code session, an interactive shell, a file browser — and the bot routes your messages to whichever window you're typing in. One `tgforge run` serves both platforms at once with the same plugins; either alone works too.

There are two ways to use it: run the **out-of-the-box app** (all bundled plugins, no code), or **define your own app** in a few lines of Python.

## 1. Out of the box

```
uv pip install "tgforge @ git+https://github.com/nguyenvlm/tgforge"
tgforge install     # wizard: name → token → workspaces → config → user service → guided group setup
```

The wizard installs the bot as a user service (systemd on Linux, launchd on macOS) and walks you through connecting it to a forum group. That's it — the default app ships every bundled plugin:

- **Claude** (`/claude`) — a Claude Code session per window, with model/account/workspace pickers, plus `!` / `!!` one-shot shell prefixes inside a Claude window (`!cmd` runs it in the window's workspace, `!!cmd` also hands the output to the agent).
- **Shell** (`/shell`) — a persistent PTY: each message is a line to stdin (slashes included), with Ctrl-C / Ctrl-D / close buttons. Starts in one of the agent's workspace roots, survives a bot restart and revives in the last cwd.
- **Files** (`/localfs`) — a tap-to-navigate file browser with `/bookmarks`, kept as the topic's bottom message; type a path (absolute, relative to the current folder, or `~/…`) to jump there.
- **gcloud** (`/gcloud`) — browserless Google Cloud sign-in.

Built-in commands work in every window: `/` opens the button menu, `/new`, `/close`, `/help`, `/usage`, `/restart`. To run it in the foreground instead of the service: `tgforge run`. To remove a bot: `tgforge uninstall [name]` (`--keep-home` keeps its state).

Only the bound owner can use the bot (you bind yourself with `/init` during setup; each platform has its own owner).

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

Writing a plugin: subclass `Topic` (one window class), declare commands with decorators (`@launch`, `@command`, `@universal`, `@on_message`, `@action`), and list the class in a `Plugin`. A window keeps live state on the instance and persistent state in `self.saved` — a dict-like view over the bot's SQLite file; a plugin never opens a file and never receives the kernel. A plugin writes standard Markdown and builds buttons with `tgforge.base.ui`; it never sees a platform. `example/mybot.py` and `example/allowlist.py` (a custom `auth(kernel, event) -> bool` hook with an admin `/allow` command) are worked examples.

## Configuration

`bot.json` (in the bot home, gitignored — it carries the tokens):

```json
{
  "token": "<telegram-bot-token>",
  "home": "~/.tgforge/mybot",
  "service": "mybot",
  "slack": {"bot_token": "xoxb-…", "app_token": "xapp-…", "owner_id": null, "control_channel": null}
}
```

`token` configures Telegram, the `slack` block configures Slack; set either or both. `home` is the bot's state dir and process cwd; the wizard puts each bot under `~/.tgforge/<name>`. `service` names the user service, which enables `/restart`. The bot writes each platform's owner (and Telegram's group, Slack's control channel) back into this file.

## Slack

Slack support is an extra: `uv pip install "tgforge[slack]"` (a `slack` block without it stops at startup with this command).

1. Create a Slack app from this manifest (api.slack.com/apps → Create New App → From a manifest):

```yaml
display_information:
  name: mybot
features:
  bot_user:
    display_name: mybot
    always_online: true
  slash_commands:
    - command: /tgf
      description: Run a tgforge command (/tgf model opus) or open the menu (/tgf)
      usage_hint: "[command] [args]"
      should_escape: false
oauth_config:
  scopes:
    bot:
      - chat:write
      - commands
      - files:read
      - files:write
      - groups:history
      - groups:read
      - groups:write
settings:
  event_subscriptions:
    bot_events:
      - message.groups
  interactivity:
    is_enabled: true
  socket_mode_enabled: true
```

2. Install it to the workspace and copy the **Bot User OAuth Token** (`xoxb-…`) into `slack.bot_token`; create an **App-Level Token** with the `connections:write` scope (`xapp-…`) for `slack.app_token`. Socket Mode keeps an outbound WebSocket open, so no public URL is needed.
3. Start the bot: it creates a private control channel (Slack's General) and saves its id. Run `/tgf init` to bind yourself as the Slack owner; you are invited to the control channel.

Each window is a private channel the bot creates and invites you to; `/close` archives it. Slack sends a message starting with `/` to its own slash commands, so commands go through `/tgf`: `/tgf model opus` runs `/model opus`, `/tgf` alone opens the menu, `/tgf /usage` is the `//usage` escape. The bot echoes each `/tgf` command into the window (`› /model opus`) before running it. A finished turn's tool calls go in a thread under its summary line; a reply in any thread reaches the window like a normal message, with the thread's first message as the replied text.

Claude-plugin extras:

- **Workspaces** (where the agent runs): `tgforge workspaces add <path-or-glob>` on the CLI, or `/workspace` in a window. Switching a topic to another root starts a fresh session there; the old one stays resumable.
- **Model / effort** (per window): `/model` switches the active model (and edits the offered list), `/effort` switches the reasoning effort — the next message respawns the session with it.
- **Background jobs**: a panel at the bottom of the window lists the agent's running background jobs; a finished job shows ✓ / ✗ (◼ when the outcome is unknown) for a short while, then drops. `/kill <job>` stops one (no name → pick from buttons); `/cancel` only interrupts the running turn.
- **Per-repo guidance**: pass `Claude(brief="…")` or drop a `session_brief.md` in the bot home — added to the session's system prompt (written to `session_notes/` in the bot home, passed as `--append-system-prompt-file`) with the bot's rules for the window's platform, so every turn runs under it, a slash prompt (a skill) included; prompts reach the agent exactly as typed.
- The note opens with `[via <platform> bot · <bot> · …]` so the agent knows where it's running; the session's environment carries `TGFORGE_WINDOW` (the window key) and `TGFORGE_BOT_DRIVEN=1`. Attached files are saved under the workspace's `tmp/attachments/`.
- **A finished turn** reads, top to bottom: the summary line `🔧 tools · 💬 notes · duration · ↓ output tokens · ctx 21% (42.4k) · 5h 3% · 7d 0%` (context used of the model's window, then the account's 5-hour and weekly quota, ⚠️ when limited), the tool calls folded under it into collapsed blocks (tap to expand), then the answer (split into as many messages as it needs, the suggested replies on the last), then any attached files. Nothing is cut. The live card shows the same context and quota while the turn runs.

## How it works

`tgforge/base/` is seven modules: `kernel.py` (the whole platform-neutral core — persistence, the plugin API, the transport with its pacing and frame-skip output, command registry, router, window lifecycle, menus), `telegram.py` and `slack.py` (one adapter per platform: every platform call and update conversion), `app.py` (the `App` + `OwnerOnly` auth), `ui.py`, `config.py`, `service.py` (the managed service + detached restart).

Each forum topic (Slack: private channel) is a window owned by one class. The kernel routes an event down a chain — the window's own commands, then universal commands, then the window's `on_message` / `on_unknown` — and each handler may consume it or return `False` to fall through. General (outside any topic; Slack: the control channel) reacts only to an `@mention` (a `/tgf` command counts as one). Window titles track state via a `title_suffix()` hook (`Shell · <cwd>`); a Claude window takes its session's title (`🤖 <session title>`).

Telegram allows a group about 20 messages a minute, so the bot paces itself below that (Slack gets the same pacer). Messages that change over time (a live card, a panel, the file browser, a shell's output, a topic title) only record their newest content and are redrawn when a slot is free — under a rate limit the bot keeps working and shows the latest state once Telegram allows it, skipping the frames in between. New messages are never skipped: they go out in order, after the wait. A window is renamed at most every 5 s (each rename posts a "renamed" notice), so quick folder clicks give one rename to the last folder.
