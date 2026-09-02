#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["httpx", "telegramify-markdown"]
# ///
"""Generate golden fixtures for the 2a render/parse differential test.

Runs the REFERENCE bot's original `_<name>` functions over cases.py and writes
their outputs to render_goldens.json. Re-run whenever the reference render layer
changes. NOT part of the hermetic test run (it needs the reference module + its
deps); the committed JSON is what the parity test reads.

    REFERENCE_BOT=/path/to/telegram_bot.py uv run tests/goldens/generate.py

The reference path comes from the env — tgforge names no consumer/workspace path.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cases  # noqa: E402

OUT = Path(__file__).resolve().parent / "render_goldens.json"


def load_reference():
    ref = os.environ.get("REFERENCE_BOT")
    if not ref:
        sys.exit("set REFERENCE_BOT to the reference telegram_bot.py path")
    spec = importlib.util.spec_from_file_location("reference_bot", ref)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# tgforge name -> reference name, for functions whose names differ across the two
_REF_ALIASES = {"has_background_launch": "has_bg_launch"}


def _call(ref, name, args):
    """Reference originals are mostly module-level; a few (render_timeline) are
    Bot methods that use no self — call those with a dummy self."""
    ref_name = _REF_ALIASES.get(name, name)
    fn = getattr(ref, "_" + ref_name, None)
    if fn is not None:
        return fn(*args)
    return getattr(ref.Bot, "_" + ref_name)(None, *args)


def main() -> None:
    ref = load_reference()
    goldens = [
        {"group": group, "name": name, "args": args, "expected": _call(ref, name, args)}
        for group, name, args in cases.ALL_CASES
    ]
    OUT.write_text(json.dumps(goldens, ensure_ascii=False, indent=2) + "\n")
    print(f"wrote {len(goldens)} goldens to {OUT}")


if __name__ == "__main__":
    main()
