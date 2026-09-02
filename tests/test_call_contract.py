"""`Transport._call` takes a zero-arg factory, never a coroutine (a flood-wait retry
re-awaits it; awaiting the same coroutine twice is a RuntimeError). Enforced here
mechanically over every call site in the package, so a future caller can't reintroduce
the bug by passing `self.bot.method(...)` directly."""

from __future__ import annotations

import ast
from pathlib import Path

PACKAGE = Path(__file__).resolve().parent.parent / "tgforge"

# a factory argument is a lambda or a reference to a callable — never a call result
ALLOWED = (ast.Lambda, ast.Name, ast.Attribute)


def _call_sites(tree: ast.AST):
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "_call"
            and node.args
        ):
            yield node


def test_every_call_site_passes_a_factory():
    bad: list[str] = []
    for path in sorted(PACKAGE.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in _call_sites(tree):
            if not isinstance(node.args[0], ALLOWED):
                bad.append(f"{path.relative_to(PACKAGE.parent)}:{node.lineno}")
    assert not bad, f"_call must receive a zero-arg factory (lambda), not a call: {bad}"


def test_scanner_catches_a_coroutine_argument():
    tree = ast.parse("async def f(self):\n    await self._call(self.bot.send_message(1, 'x'))")
    sites = list(_call_sites(tree))
    assert sites and not isinstance(sites[0].args[0], ALLOWED)
