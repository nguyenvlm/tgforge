"""Every `except` in the package re-raises, raises a bot notice, or is normal control
flow: a broad catch that only logs would hide a bot-level failure from Claude and the
user."""

from __future__ import annotations

import ast
from pathlib import Path

PACKAGE = Path(__file__).resolve().parent.parent / "tgforge"

# caught types that are normal control flow, each with its reason
NORMAL_FLOW = {
    "StopIteration": "the cut search's generator returning its cut",
    "asyncio.CancelledError": "a task stopped on purpose",
    "TimeoutError": "a wait whose deadline passed (a prompt unanswered, a process slow)",
    "KeyboardInterrupt": "the terminal user stopped an interactive command",
    "EOFError": "the terminal user closed an interactive command's input",
    "json.JSONDecodeError": "a line or file that is not JSON, skipped or read as empty",
    "OSError": "a file, pipe or process that is gone or unreadable: the probe's answer",
    "BrokenPipeError": "a CLI or shell whose pipe closed: it is gone",
    "ConnectionResetError": "a CLI or shell whose pipe closed: it is gone",
    "ProcessLookupError": "a process that already exited",
    "BlockingIOError": "a non-blocking read with nothing to read yet",
    "InterruptedError": "a read interrupted by a signal, retried on the next event",
    "subprocess.SubprocessError": "a helper process that failed: the probe's answer",
    "IndexError": "input that does not parse",
    "ValueError": "input that does not parse",
    "ImportError": "an optional extra not installed, reported at startup",
    "RuntimeError": "tgforge's own refusal (a file over the upload cap, a kill refused, no "
    "CLI to ask), reported to the user",
    "SlackApiError": "a Slack answer the caller branches on (name taken, not found)",
    "TelegramAPIError": "a Telegram answer the caller branches on (topic not found)",
}

# the calls that raise a bot notice (a log-only call such as `_log_notice` is not one)
RAISING_CALLS = {"notice_exception", "on_notice", "notice", "show_notice"}

# broad handlers that are normal control flow at their site, each with its reason
SITES = {
    ("base/kernel.py", "error_text"): "an exception whose text can't be read is named by its "
    "type alone",
    ("base/app.py", "start"): "closing a platform at shutdown: no window is served anymore",
    ("plugins/claude/driver.py", "step"): "hands the exception to the caller awaiting the step",
    ("plugins/claude/driver.py", "submit"): "a CLI that fails to launch is reported in the "
    "window: there is no session to tell",
    ("plugins/claude/driver.py", "_one_shot"): "a `!` command's failure is its result, shown "
    "in the window and handed to the agent",
    ("base/mermaid.py", "_fetch"): "a fetch that fails is named by the fallback notice of "
    "the next diagram, which goes out as code",
    ("base/mermaid.py", "_close_page"): "closing a diagram browser that already died: "
    "nothing is left to free, and the next diagram starts a fresh one",
    ("install.py", "*"): "the install CLI runs before the bot: its errors go to its terminal",
    ("cli.py", "*"): "the command line runs outside the bot: its errors go to its terminal",
}


def _caught(handler: ast.ExceptHandler) -> list[str]:
    if handler.type is None:
        return ["<bare>"]
    if isinstance(handler.type, ast.Tuple):
        return [ast.unparse(item) for item in handler.type.elts]
    return [ast.unparse(handler.type)]


def _raises_or_notices(handler: ast.ExceptHandler) -> bool:
    for node in ast.walk(ast.Module(body=handler.body, type_ignores=[])):
        if isinstance(node, ast.Raise):
            return True
        if isinstance(node, ast.Call):
            if ast.unparse(node.func).rsplit(".", 1)[-1] in RAISING_CALLS:
                return True
    return False


def _handlers():
    for path in sorted(PACKAGE.rglob("*.py")):
        tree = ast.parse(path.read_text())
        parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
        for node in ast.walk(tree):
            if isinstance(node, ast.ExceptHandler):
                scope = node
                while scope in parents and not isinstance(
                    scope, (ast.FunctionDef, ast.AsyncFunctionDef)
                ):
                    scope = parents[scope]
                yield path.relative_to(PACKAGE).as_posix(), getattr(scope, "name", ""), node


def test_every_except_re_raises_raises_a_notice_or_is_normal_flow():
    unhandled = []
    for path, function, handler in _handlers():
        if _raises_or_notices(handler):
            continue
        if all(name in NORMAL_FLOW for name in _caught(handler)):
            continue
        if (path, function) in SITES or (path, "*") in SITES:
            continue
        unhandled.append(f"{path}:{handler.lineno} {function} catches {_caught(handler)}")
    assert unhandled == [], "handlers that neither re-raise nor raise a notice:\n" + "\n".join(
        unhandled
    )


def test_the_handler_check_fails_on_a_broad_catch_that_only_logs():
    swallow = ast.parse("try:\n    f()\nexcept Exception:\n    log()").body[0].handlers[0]
    notify = ast.parse("try:\n    f()\nexcept Exception as e:\n    notice_exception(n, w, 'x', e)")
    log_only = ast.parse(
        "try:\n    f()\nexcept Exception as e:\n    _log_notice(Notice('exception', w, 'x'))"
    )
    assert not _raises_or_notices(swallow)
    assert not _raises_or_notices(log_only.body[0].handlers[0])
    assert _raises_or_notices(notify.body[0].handlers[0])
    assert not all(name in NORMAL_FLOW for name in _caught(swallow))


def test_every_allowlisted_site_still_exists():
    seen = {(path, function) for path, function, _handler in _handlers()}
    files = {path for path, _function in seen}
    for path, function in SITES:
        assert (path, function) in seen or (function == "*" and path in files), (path, function)
