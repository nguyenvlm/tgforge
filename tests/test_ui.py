"""Unit tests for the base UI components (downstream-agnostic keyboards + CTA)."""

from __future__ import annotations

from tgforge.base import ui


def test_stacked_choices_callback_data():
    kb = ui.stacked_choices(["a", "b"], "pfx")
    datas = [b.data for row in kb.rows for b in row]
    assert datas == ["pfx:0", "pfx:1"]  # index order preserved regardless of layout


def test_stacked_choices_pairs_short_labels_two_across():
    kb = ui.stacked_choices(["👤 Account", "✖️ Cancel", "🧠 Models"], "m")
    assert [len(row) for row in kb.rows] == [2, 1]  # 3 short items → 2 + 1


def test_stacked_choices_long_label_falls_back_to_one_column():
    kb = ui.stacked_choices(["/very/long/workspace/path/one", "/another/long/path/two"], "m")
    assert all(len(row) == 1 for row in kb.rows)  # long labels stay full-width


def test_stacked_choices_keeps_back_full_width():
    kb = ui.stacked_choices(["👤 Account", "🧠 Models", "⬅ Back"], "m", solo_last=True)
    assert [len(row) for row in kb.rows] == [2, 1]  # pair, then Back alone
    assert kb.rows[-1][0].data == "m:2"  # Back keeps its index


def test_blabel_end_ellipsis_keeps_the_head():
    out = ui.blabel("Commit and restart the bot once now please", 24, ellipsis="end")
    assert len(out) <= 24
    assert out.startswith("Commit and restart")  # intent at the start survives
    assert out.endswith("…") and "…" not in out[:-1]  # ellipsis only at the end


def test_suggestion_buttons_are_full_width_and_route_to_sug():
    kb = ui.suggestion_buttons(
        ["Commit now", "Something else entirely that we should really consider here at length"],
        "tok",
    )
    assert all(len(row) == 1 for row in kb.rows)  # one per row, full width
    datas = [row[0].data for row in kb.rows]
    assert datas == ["act:sug:tok:0", "act:sug:tok:1"]
    long_label = kb.rows[1][0].label
    assert long_label.startswith("Something else") and long_label.endswith("…")


def test_cta_encodes_action_and_arg():
    b = ui.cta("go", "agent.prompt", "tok123")
    assert b.data == "cta:agent.prompt:tok123"


def test_parse_cta_roundtrip():
    assert ui.parse_cta("cta:agent.prompt:tok123") == ("agent.prompt", "tok123")
    assert ui.parse_cta("cta:reload:") == ("reload", "")
    assert ui.parse_cta("sug:x:1") is None
    assert ui.parse_cta("plain") is None


def test_shell_md_is_a_standard_markdown_code_block():
    md = ui.shell_md("ls", ["has `tick` and \\ slash\n"], "exit 0")
    assert md == "```\n$ ls\nhas `tick` and \\ slash\n[exit 0]\n```"  # no MarkdownV2 escapes


def test_code_block_fence_outlasts_any_backtick_run():
    assert ui.code_block("a ``` b") == "````\na ``` b\n````"
    assert ui.code_block("plain") == "```\nplain\n```"


def test_expandable_pages_packs_with_the_callers_page_size():
    entries = ["one", "two", "three"]
    assert ui.expandable_pages("🔧 3", entries, lambda h, b, f: 0) == ui.Pages(
        "🔧 3", ["one\n\ntwo\n\nthree"]
    )
    one_per_page = ui.expandable_pages("🔧 3", entries, lambda h, b, f: len(b), limit=5)
    assert one_per_page.pages == ["one", "two", "three"]  # each body alone fits 5
