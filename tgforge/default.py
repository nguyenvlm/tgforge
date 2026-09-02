"""The default app: works out of the box with every bundled plugin. `tgforge run`
with no target builds this. For a custom bot, write your own module (see
example/mybot.py) and `tgforge run yourmodule:app`.

Restart is a built-in core command; the `!` / `!!` one-shot prefixes belong to the
claude plugin. Authorization defaults to OwnerOnly (see example/allowlist.py to
extend it).
"""

from __future__ import annotations

from tgforge.base.app import App
from tgforge.base.config import BotConfig
from tgforge.plugins.claude import Claude
from tgforge.plugins.gcloud import Gcloud
from tgforge.plugins.localfs import Localfs
from tgforge.plugins.shell import Shell


def build_default_app(config: str | BotConfig = "bot.json") -> App:
    app = App(config)
    app.include(Claude())
    app.include(Shell())
    app.include(Localfs())
    app.include(Gcloud())
    return app
