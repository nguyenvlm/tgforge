# tgforge

Run agents and tools from Telegram or Slack. tgforge turns a Telegram forum group — or a Slack workspace — into a desktop: each topic (Slack: each private channel) is a window — a Claude Code session, an interactive shell, a file browser — and the bot routes your messages to whichever window you're typing in. One `tgforge run` serves both platforms at once with the same plugins; either alone works too.

There are two ways to use it: run the **out-of-the-box app** (all bundled plugins, no code), or **define your own app** in a few lines of Python.

## 1. Out of the box

```
uv pip install "tgforge @ git+https://github.com/nguyenvlm/tgforge"
tgforge install     # wizard: name → token → workspaces → config → user service → guided group setup
```

The wizard installs the bot as a user service (systemd on Linux, launchd on macOS) and walks you through connecting it to a forum group. That's it — the default app ships every bundled plugin:

- **Claude** (`/claude`) — a Claude Code session per window, with model/account/workspace pickers, plus `!` / `!!` one-shot shell prefixes inside an agent window (`!cmd` runs it in the window's workspace, `!!cmd` also hands the output to the agent).
- **agy** (`/agy`) — an agy (Antigravity CLI) session per window, with the same features as a Claude window (see agy-plugin extras below); needs `agy` installed and signed in.
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

## Mermaid diagrams

On Telegram, a ```` ```mermaid ```` code block in a message is drawn as an image in its place (HTML labels such as `<sub>` and `<br>` included). It is part of the default install: tgforge draws each diagram with the mermaid.js bundle in a headless Chromium driven by Playwright. Neither file ships in the package; tgforge pins mermaid.js by version and sha256 and fetches both once into the bot home, under `<home>/mermaid/` (the bundle, about 5 MB, and `browsers/`, about 270 MB):

```
tgforge fetch-mermaid --app mybot
```

`tgforge install` runs this step itself, and the bot fetches whichever file is missing at startup, in the background. A bundle whose sha256 differs from the pinned one is deleted and never loaded. On a bare Linux the browser may also need its system libraries: `python -m playwright install-deps chromium-headless-shell` (it installs system packages, so it asks for root).

The browser starts on the first diagram (about 1.3 s) and closes after 5 idle minutes (about 400–700 MB while open). Offline, or whenever a file is missing or a diagram does not draw, the block goes out as code with a fallback notice saying why (a Claude window's agent gets it with the next prompt, any other window shows it as a message), and the bot tries the fetch again at most once every 5 minutes. Slack shows the block as code.

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

Each window is a private channel the bot creates and invites you to; `/close` archives it. Slack sends a message starting with `/` to its own slash commands, so commands go through `/tgf`: `/tgf model opus` runs `/model opus`, `/tgf` alone opens the menu, `/tgf /usage` is the `//usage` escape. The bot echoes each `/tgf` command into the window (`› /model opus`) before running it. A message with a space before the slash (` /model opus`) is not a Slack command, so it reaches the bot as a typed command, but only in a window that takes commands: the control channel ignores it, and a Shell window takes it as input. A message led by the bot's mention (`@mybot /model opus`) runs as a command everywhere, like `/tgf`. With several tgforge apps in one workspace (one per machine), use these forms: each app has its own control channel and windows, and Slack sends `/tgf` only to the app installed last. A finished turn's tool calls go in a thread under its summary line; a reply in any thread reaches the window like a normal message, with the thread's first message as the replied text.

Claude-plugin extras:

- **Workspaces** (where the agent runs): `tgforge workspaces add <path-or-glob>` on the CLI, or `/workspace` in a window — one list for Claude and agy windows alike. Switching a topic to another root starts a fresh session there; the old one stays resumable.
- **Model / effort** (per window): `/model` switches the active model (and edits the offered list), `/effort` switches the reasoning effort — the next message respawns the session with it.
- **Background jobs**: a panel at the bottom of the window lists the agent's running background jobs; a finished job shows ✓ / ✗ (◼ when the outcome is unknown) for a short while, then drops. `/kill <job>` stops one (no name → pick from buttons); `/cancel` only interrupts the running turn.
- **Per-repo guidance**: pass `Claude(brief="…")` or drop a `session_brief.md` in the bot home — added to the session's system prompt (written to `session_notes/` in the bot home, passed as `--append-system-prompt-file`) with the bot's rules for the window's platform, so every turn runs under it, a slash prompt (a skill) included; prompts reach the agent exactly as typed.
- The note opens with `[via <platform> bot · <bot> · …]` so the agent knows where it's running; the session's environment carries `TGFORGE_WINDOW` (the window key) and `TGFORGE_BOT_DRIVEN=1`. Attached files are saved under the workspace's `tmp/attachments/`.
- **A finished turn** reads, top to bottom: the summary line `🔧 tools · 💬 notes · duration · ↓ output tokens · ctx 21% (42.4k) · 5h 3% · 7d 0%` (context used of the model's window, then the account's 5-hour and weekly quota, ⚠️ when limited), the tool calls folded under it into collapsed blocks (tap to expand), then the answer (split into as many messages as it needs, the suggested replies on the last), then any attached files. Nothing is cut. The live card shows the same context and quota while the turn runs.

agy-plugin extras: `/agy` opens an agy (Antigravity CLI) window with the same features as a Claude window: the live card, finalized turns, the message buffer, mid-turn messages, `/stop` `/cancel` `/kill` `/clear` `/cli` `/rename` `/account` `/mode` `/status` `/model` `/effort` `/workspace`, slash commands as skills, `!` / `!!`, the background-jobs panel and the transcript mirror. The default app includes it; in your own app, `app.include(Agy())` (`from tgforge.plugins.agy import Agy`; it takes the same `roots`, `model`, `models`, `brief`, `permission_mode` arguments as `Claude`); both plugins run in one app.

- **Needs** the `agy` CLI on `PATH` (or in `~/.local/bin`), signed in. Each window runs one `agy acp` process (the Agent Client Protocol server) with one session in it.
- **Accounts**: the default account is the real `~/.gemini`; another account lives in its own home, `~/.agy-<name>` (its data in `~/.agy-<name>/.gemini/antigravity-cli/`). `/login` signs an account in (the sign-in URL comes to the chat; paste the code back within 60 s); `/account` switches a window's account, carrying its session along. On the PC, `agy-as <name> …` (installed in `~/.local/bin`) runs agy under an account, and `/cli` gives the `cd <workspace> && [HOME=~/.agy-<name> ]agy --conversation <id>` line; agy asks to trust the folder it runs from the first time.
- **Modes** (`/mode`, app-wide): `auto` (the default), `default`, `accept-edits`, `plan`, and `skip` (the process restarts with `--dangerously-skip-permissions`). The window answers every permission agy asks for with deny, and the denied tool and its command show in the turn's tool calls.
- **Effort** (`/effort`): `low`, `medium`, `high`, `xhigh`. With no model list set, `/model` offers the models agy lists for the session.
- **Differences from a Claude window**: a message sent while a turn runs always joins that turn (agy takes it after its current step); `/cancel` also stops the session's background jobs, and a `/cancel` during a turn a finished background job started ends that turn at once as cancelled (agy sends nothing after it); `/kill` stops a job by killing its process tree; the window never ends an agy process that runs a job on its own (agy's process takes its jobs down with it), so `/clear` and a workspace or account switch wait until no job runs, the idle release waits for the last job, and a switch to or from `/mode skip` applies after the jobs end (`/mode` and `/status` say which mode still runs); closing the window with `/stop` stops its running jobs, and its confirm says so; `↓ tokens` counts per model step; the context size comes from agy; `/rename` with no name uses agy's own session title; attached images also go to agy as images.
- **Quota**: after a turn the window polls the account's quota (`agy --output-format json -p=/usage`, at most once a minute per account) and shows its model's group as `5h P% · 7d P%` used (⚠️ first when a limit is used up; only `7d` for a group with no 5-hour limit; none for a model no group lists). A failed or hung poll (stopped after 30 s) keeps the last value and posts one notice in General until a poll works again.
- **Limits**: the workspace's `system_prompt.txt` is appended to agy's system prompt, not put in its place (agy can only append), and agy sessions on the PC run without it; no live token count within a step.

## How it works

`tgforge/base/` is seven modules: `kernel.py` (the whole platform-neutral core — persistence, the plugin API, the transport with its pacing and frame-skip output, command registry, router, window lifecycle, menus), `telegram.py` and `slack.py` (one adapter per platform: every platform call and update conversion), `app.py` (the `App` + `OwnerOnly` auth), `ui.py`, `config.py`, `service.py` (the managed service + detached restart).

Each forum topic (Slack: private channel) is a window owned by one class. The kernel routes an event down a chain — the window's own commands, then universal commands, then the window's `on_message` / `on_unknown` — and each handler may consume it or return `False` to fall through. General (outside any topic; Slack: the control channel) reacts only to an `@mention` (a `/tgf` command counts as one). Window titles track state via a `title_suffix()` hook (`Shell · <cwd>`); a Claude window takes its session's title (`🤖 <session title>`).

Telegram allows a group about 20 messages a minute, so the bot paces itself below that (Slack gets the same pacer). Messages that change over time (a live card, a panel, the file browser, a shell's output, a topic title) only record their newest content and are redrawn when a slot is free — under a rate limit the bot keeps working and shows the latest state once Telegram allows it, skipping the frames in between. New messages are never skipped: they go out in order, after the wait. A window is renamed at most every 5 s (each rename posts a "renamed" notice), so quick folder clicks give one rename to the last folder.
