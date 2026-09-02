"""Example app module. Run it with:

    tgforge run example.mybot:app

Needs a bot.json beside where you run it (copy bot.json.example).
"""

from tgforge import App
from tgforge.plugins.claude import Claude
from tgforge.plugins.gcloud import Gcloud
from tgforge.plugins.localfs import Localfs
from tgforge.plugins.shell import Shell

app = App(config="bot.json")
# model/models are Claude-app config, set here (not in base bot.json). `brief=` adds
# app-specific guidance to every turn (e.g. your repo's conventions); left unset, the
# plugin reads `session_brief.md` from the bot home instead:
app.include(Claude(model="claude-opus-4-8[1m]", brief="Follow this repo's house rules."))
app.include(Shell())
app.include(Localfs(bookmarks=["~/projects", "/var/log"]))  # start folders + ⭐ jump targets
app.include(Gcloud())
