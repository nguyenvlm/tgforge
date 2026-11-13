"""The adapter boundary: only `base/telegram.py`, `base/slack.py` and `testing.py` import a
platform library, and only `base/telegram.py` the MarkdownV2 converter; the kernel, the
app, `ui.py` and every plugin import none of them."""

from __future__ import annotations

import ast
from pathlib import Path

PACKAGE = Path(__file__).resolve().parent.parent / "tgforge"
ADAPTERS = {"base/telegram.py", "base/slack.py", "testing.py"}
ALLOWED = {"aiogram": ADAPTERS, "slack_sdk": ADAPTERS, "telegramify_markdown": {"base/telegram.py"}}
LIBRARIES = set(ALLOWED)


def _platform_imports(source: str) -> list[str]:
    found = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found += [a.name for a in node.names if a.name.split(".")[0] in LIBRARIES]
        elif isinstance(node, ast.ImportFrom) and node.module:
            if node.module.split(".")[0] in LIBRARIES:
                found.append(node.module)
    return found


def test_only_the_adapters_and_the_test_harness_import_a_platform_library():
    offenders = {}
    for path in sorted(PACKAGE.rglob("*.py")):
        rel = path.relative_to(PACKAGE).as_posix()
        hits = [
            h for h in _platform_imports(path.read_text()) if rel not in ALLOWED[h.split(".")[0]]
        ]
        if hits:
            offenders[rel] = hits
    assert offenders == {}


def test_the_scanner_catches_a_platform_import():
    assert _platform_imports("from aiogram.types import Message") == ["aiogram.types"]
    assert _platform_imports("def f():\n    import slack_sdk.web") == ["slack_sdk.web"]
    assert _platform_imports("from telegramify_markdown import x") == ["telegramify_markdown"]
    assert _platform_imports("import asyncio") == []


def test_plugins_reach_the_adapter_only_through_the_public_property():
    private = []
    for path in sorted((PACKAGE / "plugins").rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Attribute) and node.attr == "_tx":
                private.append(f"{path.relative_to(PACKAGE).as_posix()}:{node.lineno}")
    assert private == []
