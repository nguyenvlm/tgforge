"""Claude-domain rendering: tool lines, the live-status head, the turn timeline,
and the `[[suggest]]`/`[[attach]]` answer markers. Builds on the base
downstream-agnostic primitives in `tgforge.base.ui`.
"""

from __future__ import annotations

import re
from pathlib import Path

from tgforge.base.ui import fmt_duration


def fmt_tokens(n: int) -> str:
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


SPINNER = "✶✸✹✺✹✸"
# Claude Code's own spinner gerunds, extracted from the CLI binary.
WORDS = tuple(
    """
Accomplishing Actioning Actualizing Architecting Baking Beaming Befuddling
Billowing Blanching Bloviating Boogieing Boondoggling Booping Bootstrapping
Brewing Bunning Burrowing Calculating Canoodling Caramelizing Cascading
Catapulting Cerebrating Channeling Channelling Choreographing Churning
Clauding Coalescing Cogitating Combobulating Composing Computing Concocting
Considering Contemplating Cooking Crafting Creating Crunching Crystallizing
Cultivating Deciphering Deliberating Determining Dilly-dallying
Discombobulating Doing Doodling Drizzling Ebbing Effecting Elucidating
Embellishing Enchanting Envisioning Fermenting Fiddle-faddling Finagling
Flambéing Flibbertigibbeting Flowing Flummoxing Fluttering Forging Forming
Frolicking Frosting Gallivanting Galloping Garnishing Generating Germinating
Gesticulating Gitifying Grooving Gusting Harmonizing Hashing Hatching
Herding Honking Hullaballooing Hyperspacing Ideating Imagining Improvising
Incubating Inferring Infusing Ionizing Jitterbugging Julienning Kneading
Leavening Levitating Lollygagging Manifesting Marinating Meandering
Metamorphosing Misting Moonwalking Moseying Mulling Musing Mustering
Nebulizing Nesting Newspapering Noodling Nucleating Orbiting Orchestrating
Osmosing Perambulating Percolating Perusing Philosophising Photosynthesizing
Pollinating Pondering Pontificating Pouncing Precipitating Prestidigitating
Processing Proofing Propagating Puttering Puzzling Quantumizing
Razzle-dazzling Razzmatazzing Recombobulating Reticulating Roosting
Ruminating Sautéing Scampering Schlepping Scurrying Seasoning Shenaniganing
Shimmying Simmering Skedaddling Sketching Slithering Smooshing Sock-hopping
Spelunking Spinning Sprouting Stewing Sublimating Swirling Swooping
Symbioting Synthesizing Tempering Thinking Thundering Tinkering Tomfoolering
Topsy-turvying Transfiguring Transmuting Twisting Undulating Unfurling
Unravelling Vibing Waddling Wandering Warping Whatchamacalliting
Whirlpooling Whirring Whisking Wibbling Working Wrangling Zesting Zigzagging
""".split()
)

_TOOL_ICONS = {
    "Bash": "💻",
    "Read": "📖",
    "Edit": "✏️",
    "Write": "📄",
    "NotebookEdit": "📓",
    "Skill": "⚡",
    "Grep": "🔍",
    "Glob": "🔍",
    "WebFetch": "🌐",
    "WebSearch": "🌐",
    "TaskCreate": "📋",
    "TaskUpdate": "📋",
    "TaskGet": "📋",
    "TaskList": "📋",
}

_SUGGEST_RE = re.compile(r"(?m)^[ \t]*\[\[suggest\]\][ \t]*(.+?)[ \t]*$")
_ATTACH_RE = re.compile(r"(?m)^[ \t]*\[\[attach\]\][ \t]*(.+?)[ \t]*$")


def tool_line(name: str, tool_input) -> str:
    """One human line per tool call, icon-bulleted: '📖 Read: io.py'."""
    icon = _TOOL_ICONS.get(name, "🔧")
    if not isinstance(tool_input, dict):
        return f"{icon} {name}"
    if name == "Bash":
        detail = tool_input.get("description") or tool_input.get("command", "")
    elif name in ("Read", "Edit", "Write", "NotebookEdit"):
        detail = Path(tool_input.get("file_path", "")).name
    elif name == "Skill":
        detail = tool_input.get("skill", "")
    elif name in ("Grep", "Glob"):
        detail = tool_input.get("pattern", "")
    elif name == "WebFetch":
        detail = tool_input.get("url", "")
    elif name == "WebSearch":
        detail = tool_input.get("query", "")
    else:
        detail = next((v for v in tool_input.values() if isinstance(v, str) and v.strip()), "")
    detail = " ".join(str(detail).split())
    if len(detail) > 80:
        detail = detail[:77] + "..."
    return f"{icon} {name}: {detail}" if detail else f"{icon} {name}"


def status_head(seed: int, spin: int, elapsed: int, tokens: int) -> str:
    """The one live-status line — initial ack and heartbeat share it. spin
    advances the frame per holder edit; the word drifts on wall clock."""
    frame = SPINNER[spin % len(SPINNER)]
    word = WORDS[(seed + elapsed // 20) % len(WORDS)]
    return f"{frame} {word}… ({fmt_duration(elapsed)} · ↓ {fmt_tokens(tokens)} tokens)"


def render_timeline(
    timeline: list[tuple[str, str]], drop_last_text: bool
) -> tuple[list[str], int, int]:
    """Event lines in order — tool calls, 🧠 thinking, 💬 text — dropping adjacent
    identical tools. drop_last_text excludes the final text entry (the turn's
    answer). Returns (lines, tool_count, narration_count)."""
    last_text = -1
    if drop_last_text:
        for i, (kind, _) in enumerate(timeline):
            if kind == "text":
                last_text = i
    lines: list[str] = []
    n_tools = 0
    n_narr = 0
    prev: tuple[str, str] | None = None
    for i, (kind, payload) in enumerate(timeline):
        if kind == "tool":
            if prev == ("tool", payload):  # collapse repeats
                continue
            n_tools += 1
            lines.append(payload)
        elif kind == "thinking":
            if payload:
                lines.append(f"🧠 {payload}")
        else:  # text
            narr = strip_suggest(payload) if i != last_text else ""
            if narr:
                n_narr += 1
                lines.append(f"💬 {narr}")
        prev = (kind, payload)
    return lines, n_tools, n_narr


def strip_suggest(text: str) -> str:
    """Drop any `[[suggest]]`/`[[attach]]` line — markers must never render."""
    return _ATTACH_RE.sub("", _SUGGEST_RE.sub("", text)).strip()


def extract_attachments(text: str) -> tuple[str, list[str]]:
    """Pull every `[[attach]] /path [| /path ...]` line out of the answer; returns
    the answer without them and the path list (empty when absent)."""
    paths: list[str] = []
    for m in _ATTACH_RE.finditer(text):
        paths.extend(p.strip() for p in m.group(1).split("|") if p.strip())
    return (_ATTACH_RE.sub("", text).strip(), paths) if paths else (text, [])


def extract_suggestions(text: str) -> tuple[str, list[str]]:
    """Pull a `[[suggest]] a | b | c` line out of the answer; returns the answer
    without it and the option list (empty when absent)."""
    m = _SUGGEST_RE.search(text)
    if not m:
        return text, []
    options = [o.strip() for o in m.group(1).split("|") if o.strip()]
    clean = (text[: m.start()] + text[m.end() :]).strip()
    return clean, options[:6]
