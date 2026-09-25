# tgforge — agent guide

Agent-facing architecture + invariants for working in this repo. `CLAUDE.md` symlinks here. User-facing usage lives in `README.md`; this file is the "why" and the rules a change must not break.

## Dev basics

- Branches: `main` (default) + `dev`, both long-lived and protected (PR-only). Land via a feature branch → PR into `dev`; `dev` → `main` promotes.
- Tooling: uv-managed venv (Python 3.12, `uv pip install -e ".[dev]"`); ruff only (`ruff format` + `ruff check --fix`), format only files you changed.
- Tests run hermetically through the repo venv (`env -u VIRTUAL_ENV .venv/bin/python -m pytest`) — MockBot via `tgforge.testing.TestClient`, no network or token.
- Design conventions: few files (framework core in `base/kernel.py`, satellite one-concept modules are the exception); a name says what it does; repo text stays generic — neutral example names (`mybot`), no personal identifiers, no live bot names.

## Claude window: turn lifecycle & concurrency (`plugins/claude/driver.py`)

The most defect-prone area — message ordering and turn boundaries. The model below is essential; changes here need order-asserting MockBot tests.

Tasks and state:
- `submit()` runs in the message-handler task and holds `self.lock`. It acks + queues an inbound prompt (`pending_writes`) and writes it to the CLI stdin.
- `_reader()` runs as its own task, draining the CLI stdout event stream; its `result` branch calls `_finalize_turn()`.
- A single live card per turn is the `holder_id` message; `_heartbeat()` repaints it (the live panel) while the turn runs.
- Mid-turn messages: the CLI merges a prompt sent while a turn runs into that turn (one `result`), echoing it as a prompt-replay `user` event. The reader's fold path deletes the "queued ✓" ack and reorders the card (`_reorder_holder`).

Turn boundary (the invariant that ordering hangs on):
- The `result` event IS the turn boundary. A message ack'd before it belongs to this turn; ack'd at/after it belongs to the next turn.
- `_finalize_turn()` takes `self.lock` so the boundary is atomic against `submit()` — without it, ack+queue and settle interleaved and a card could finalize in the wrong place.
- `turn_seq` (monotonic, bumped once per `result`) stamps each `pending_writes` entry at ack time; finalize partitions entries by it (`<= boundary` = this turn, `> boundary` = next turn).
- The lock is held across I/O only in the rare reorder case (this-turn mid-turn acks exist); the common path is lock-and-go — keep it that way (no network await in the critical section by default).

### Message-ordering invariant

The finalized reply is the BOTTOM-MOST of the turn's messages. Everything posted while the turn runs stays ABOVE it:
- the user's mid-turn messages (the turn folds their acks and reorders the card below them),
- a command's mid-turn note (e.g. `/kill`'s "stopping…": sent under `self.lock`, then the card is re-sent below it), and
- the turn's timeline/events.

A message ack'd at/after the boundary is the next turn's and stays below the finalized reply (card above it).

The background-jobs panel is not a turn message: it sits below everything, the finalized reply included. The kernel records the newest message id per thread (every `Transport._call` result, every inbound message, and a successful topic rename, whose service message the bot never receives); a panel paint that finds a newer id than the panel's re-sends the panel silently (`disable_notification`) and droppably (under flood-wait the old panel stays and the next tick retries), then deletes the old one. The send, the id swap and the delete run as one task shielded from the painter's cancel, and every paint first waits for one still in flight.

### Finalized-turn format

At settle, a turn renders as:
- the timeline FOLDED into a collapsed expandable bubble (`ui.expandable` — only the header line `🔧 · 💬 · duration · tokens` shown, details hidden until tapped), ABOVE
- the REPLY, which is the finalized message (the bottom-most of the turn's messages, carries the suggestion buttons).

Rules:
- Never leave the live expanded timeline frame (the heartbeat's inline working view) as the finalized card — always replace it with the folded expandable.
- Fits one Telegram message → one card (folded bubble on top, reply below). Overflows → split into sequential messages in that order (folded timeline above, reply below); the reply stays the bottom finalized message.
- Truncating the folded timeline body is fine (it's collapsed anyway).

## Background jobs (`plugins/claude/background.py`)

- A job is registered from its Bash launch result (`backgroundTaskId` + its `tasks/<id>.output` path). It ends when a completion event names it, when a Read of its output file finds the exit marker, or when a probe reads the exit marker the CLI appends to the output file (`[exited with code N]`, `[killed]`); with no marker, a job past its grace whose file nothing holds open ends as ◼.
- The panel is its own message, placed per the message-ordering invariant. It lists running jobs, plus each finished job's ✓ / ✗ / ◼ for `FINISHED_LINGER` seconds.
- `/kill <job>` stops a job through the CLI that owns it: a `stop_task` control request, which kills the job's whole process tree. With no live CLI, `kill_file_holders` (kernel) kills the processes holding the output file, every process in a session whose leader is one of them (the job's shell; a reader in another terminal brings only itself), and all their descendants, and refuses a set that reaches the bot's own group or session. Its mid-turn note follows the message-ordering invariant.

## Mid-session config switching

Two scopes:
- Per-window (per topic): model, effort, account, workspace. Stored on the topic, saved per window.
- App-wide (the plugin): permission mode; and the pools — the model list and workspace roots.

Uniform switch mechanism: set the value, then kill an idle proc so the next message respawns with it (an in-flight turn is untouched). Every switch menu shows the current value with a ✓.

Pool editing (add/remove/reset) belongs only where the pool is user-editable (model list, workspace roots). Fixed sets (permission mode, effort) are switch-only.

Command naming is singular — each command's primary act is selecting the active value for the session: `/account`, `/mode`, `/model`, `/effort`, `/workspace`. The typed forms (`/model add|rm|reset`, `/workspace add|rm`) keep working from the keyboard.
