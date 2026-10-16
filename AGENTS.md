# tgforge — agent guide

Agent-facing architecture + invariants for working in this repo. `CLAUDE.md` symlinks here. User-facing usage lives in `README.md`; this file is the "why" and the rules a change must not break.

## Dev basics

- Branches: `main` (default) + `dev`, both long-lived and protected (PR-only). Land via a feature branch → PR into `dev`; `dev` → `main` promotes.
- Tooling: uv-managed venv (Python 3.12, `uv pip install -e ".[dev]"`); ruff only (`ruff format` + `ruff check --fix`), format only files you changed.
- Tests run hermetically through the repo venv (`env -u VIRTUAL_ENV .venv/bin/python -m pytest`) — MockBot via `tgforge.testing.TestClient`, no network or token.
- Design conventions: few files (framework core in `base/kernel.py`, satellite one-concept modules are the exception); a name says what it does; repo text stays generic — neutral example names (`mybot`), no personal identifiers, no live bot names.

## Frame-skip output (`base/kernel.py` `Transport`)

A kernel invariant every plugin follows: on a rate limit the bot stops calling Telegram and keeps working locally (like a game capped at the GPU's frame rate).

- Live messages — anything updated over time (the Claude live card, the background-jobs panel, the Files view, the Shell panel, the `!` output, the mirror spinner) — go through `show_frame` / `show_bottom_frame`: the call only records the newest wanted content and returns at once (no future). One frame task pushes each recorded content when a pacer slot is spare (no essential call pending) and no flood-wait runs; content replaced before it was pushed is skipped, never sent; content equal to what landed makes no call. A finalizing edit goes through `land_frame`: it is pushed first, keeps that priority through flood-waits until it lands, and its future says whether Telegram accepted it. A loop reading local input records at most one frame per interval (the `!` pump: one per `EDIT_INTERVAL`), never one per line. Closing a window drops its pending frames.
- Topic titles: `show_title` (via `refresh_title()` / `rename_topic`) is the same mechanism plus at least `TITLE_MIN_INTERVAL` (5 s) between renames of one topic, since each rename posts a "topic renamed" service message; a burst gives one rename to the last name, an unchanged name makes no call. An explicit `/rename` passes `immediate` (no spacing).
- New messages and deletes (`_call`: `send`, `send_rich`, `send_md`, `delete`, one-off edits) are never skipped and go out in issue order. They are issued by the task that owns the sequence, never by a loop that reads local input (the CLI reader, the mirror loop, a shell's output pump): such a loop records frames or queues send steps.
- Flood-wait: on `retry_after` no call is made until the deadline (`flood_until`); every call (essential or frame push) waits for it, takes its pacer slot, and re-checks the deadline before the attempt (`_clear_to_call`: a flood recorded during the slot wait sends the slot back and starts over). An essential call waits the full `retry_after` and retries until it lands — never early. Frames keep collecting their newest content meanwhile.
- Other errors (`_attempt`): a network error or a Telegram 5xx is retried up to `TRANSIENT_RETRIES` (3) times, 1, 2 and 4 s apart, with a WARNING per retry, then given up with a WARNING; a 4xx gets one try. A failed bottom-frame re-send is retried up to `BOTTOM_RESEND_ATTEMPTS` (3) times, then waits for the next content or newer message.
- A button-tap answer (`callback.answer()`) takes no pacer slot (it posts no message) but still waits out a flood-wait.
- No plugin keeps its own pacing, retry, skip, or no-op check: those live here only.

## Claude window: turn lifecycle & concurrency (`plugins/claude/driver.py`)

The most defect-prone area — message ordering and turn boundaries. The model below is essential; changes here need order-asserting MockBot tests.

Tasks and state:
- `submit()` runs in the message-handler task and holds `self.lock`. It queues an inbound prompt (`pending_writes`; mid-turn, its "queued ✓" ack is a send step) and writes it to the CLI stdin.
- `_reader()` runs as its own task, draining the CLI stdout event stream; its `result` branch calls `_finalize_turn()`. It never sends: every message of the window goes out through `_send_in_order` — send steps, each its own task run after the ones queued before it — so screen order is queue order and the reader keeps reading while Telegram is slow or flood-waited. Every message a window posts or deletes goes through its `Topic.run_message_call` hook — the `Topic` send family (`send`, `send_rich`, `send_md`, `send_file`, `send_photo`, `delete`), a `Context` in that window, and the kernel's menus, prompts (`ask_buttons`, `menu`, `ask_text`, `pick_dir`) and help reply. The default runs the call at once; `ClaudeTopic` runs it as a send step (directly when already inside one), so command replies and menus (`/cancel`, `/kill`'s picker, an inline `/status`, `ctx.send`) are ordered the same way; a cancelled step releases its waiting caller. A prompt's tap future is registered before its message is sent, so a tap that comes the moment it shows is never lost.
- A turn is opened by `_open_holder()` (every turn-entry path: submit, a background-completion turn, a prompt replay, the result fallback): fresh `TurnState`, `turn_open`, and the live card queued as a send step behind the previous turn's output, so the next turn's card opens only after the previous settle's last message is out. The card id lives on the turn (`turn.card_id`); `_heartbeat()` paints the open turn's card as frames, every `EDIT_INTERVAL`, and exits when the turn closes.
- Mid-turn messages: the CLI merges a prompt sent while a turn runs into that turn (one `result`), echoing it as a prompt-replay `user` event. The reader's fold path queues the delete of the "queued ✓" ack and a card move below it (`_reorder_holder`). A queued next-turn message that starts a turn has its ack deleted and gets a fresh card at the bottom, replying to the user's message.

Turn boundary (the invariant that ordering hangs on):
- The `result` event IS the turn boundary. A message ack'd before it belongs to this turn; ack'd at/after it belongs to the next turn.
- `_finalize_turn()` takes `self.lock` so the boundary is atomic against `submit()`: it bumps `turn_seq`, partitions `pending_writes`, closes the turn, and queues its settle (`_settle_turn`) as a send step.
- `turn_seq` (monotonic, bumped once per `result`) stamps each `pending_writes` entry at ack time; finalize partitions entries by it (`<= boundary` = this turn, `> boundary` = next turn).
- No Telegram call happens under `self.lock`: `submit()` holds it across the CLI spawn and the stdin write only, `_finalize_turn()` and `_send_turn_note()` only queue send steps.

### Message-ordering invariant

The finalized reply is the BOTTOM-MOST of the turn's messages. Everything posted while the turn runs stays ABOVE it:
- the user's mid-turn messages (the turn folds their acks and moves the card below them; a this-turn message whose replay never came has its ack deleted and the turn's output sent below it at settle),
- a command's mid-turn note (e.g. `/kill`'s "stopping…": queued under `self.lock`, then the card is re-sent below it), and
- the turn's timeline/events.

A message ack'd at/after the boundary is the next turn's and stays below the finalized reply.

The background-jobs panel is not a turn message: it sits below everything, the finalized reply included. It is a bottom frame (`show_bottom_frame`): the kernel records the newest message id per thread (every essential call's result, every inbound message, and a landed topic rename, whose service message the bot never receives), and any id newer than the panel's schedules one silent re-send of the panel below it, retried until it lands, then the old copy's delete. This runs in the frame task, independent of the heartbeat, the between-turn updater, or the CLI process; one INFO log line per re-anchor and per dropped attempt.

### Finalized-turn format

At settle, a turn renders as new messages in this order, each piece waiting out flood-waits and retried until it lands (no gap, no skip); a network error or 5xx gets the bounded retries above:
- the tool-call part: one or more messages, each one collapsed expandable block (`ui.expandable_pages`); the first carries the summary line `🔧 N · 💬 N · duration · tokens · ctx P% (T) · 5h P% · 7d P%`; timeline entries are packed into as few messages as fit; nothing is cut — an entry longer than a message is split across messages; a turn with no tools still gets the summary-only message;
- the answer part: one or more messages (MarkdownV2, split to fit); an empty answer is `(no reply)`; the suggestion buttons ride the last answer message;
- the `[[attach]]` files.

Rules:
- The live card always becomes tool-call message 1 (a `land_frame` edit); if that edit fails, the card is deleted and message 1 sent fresh — the live working frame never stays as a finalized card.
- No caps: no tool-detail cut, no answer-message cap.
- The live card while the turn runs shows the tail only, under the status line `✶ Word… (12s · ↓ 1.2k tokens · ctx 21% (42.4k) · 5h 3% · 7d 0%)`.
- Context tokens = `input_tokens + cache_creation_input_tokens + cache_read_input_tokens` of the main agent's last `message_start`. The context window comes from `result.modelUsage[<model>].contextWindow`, saved per window (before the first result: 1M for a model id ending `[1m]`, else 200k; a model switch clears it). Quota is the newest `rate_limit_event`'s `unifiedWindows`, kept per account (config dir) on the plugin, so every window on that account shows it; ⚠️ leads the quota part when `status` is not `allowed`. There are no separate quota alert messages.

## Background jobs (`plugins/claude/background.py`)

- A job is registered from its launch result — a background Bash or an Agent launch (`backgroundTaskId` + its `tasks/<id>.output` path). It ends when a completion event names it, when a Read of its output file finds the exit marker, or when a probe reads the exit marker the CLI appends to the output file (`[exited with code N]`, `[killed]`); with no marker, a job past its grace whose file nothing holds open ends as ◼.
- The panel is its own message, placed per the message-ordering invariant. It lists running jobs, plus each finished job's ✓ / ✗ / ◼ for `FINISHED_LINGER` seconds. The updater (`_background_updater`) only ticks its clock and prunes, while it shows rows — started when a job registers or a settle leaves rows, never stopped by a turn or the CLI process ending.
- `/kill <job>` stops a job through the CLI that owns it: a `stop_task` control request, which kills the job's whole process tree. With no live CLI, `kill_file_holders` (kernel) kills the processes holding the output file, every process in a session whose leader is one of them (the job's shell; a reader in another terminal brings only itself), and all their descendants, and refuses a set that reaches the bot's own group or session. Its mid-turn note follows the message-ordering invariant.

## Mid-session config switching

Two scopes:
- Per-window (per topic): model, effort, account, workspace. Stored on the topic, saved per window.
- App-wide (the plugin): permission mode; and the pools — the model list and workspace roots.

Uniform switch mechanism: set the value, then kill an idle proc so the next message respawns with it (an in-flight turn is untouched). `/clear` and a workspace switch (a fresh session) drop it the same way. Every switch menu shows the current value with a ✓. `/model` reset restores the app's `Claude(models=[…])` list.

Pool editing (add/remove/reset) belongs only where the pool is user-editable (model list, workspace roots). Fixed sets (permission mode, effort) are switch-only.

Command naming is singular — each command's primary act is selecting the active value for the session: `/account`, `/mode`, `/model`, `/effort`, `/workspace`. The typed forms (`/model add|rm|reset`, `/workspace add|rm`) keep working from the keyboard.
