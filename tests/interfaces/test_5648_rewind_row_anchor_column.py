"""Tier 1: #5648 — `rewind_row_text`'s own contract for its 4th column
(the anchor preview, #1547's own field on every `list_rewind_points` row).

Owner-hit (2026-09-02, verbatim): "/rewind 出てくる候補は seq 番号しか出て
ないからどこに戻せば良いか不明。当時のプロンプトの先頭行だけでも良いから
ヒント表示対応して" — the `anchor` field already existed on every row
(`AgentRegistry.list_rewind_points`, #1547); this picker's own rendering
function was simply dropping it. `rewind_row_text` is pure (no Textual
mounting needed), so its own contract is tested directly here — a Tier 1
closed-form function: same input, same output, every time.
"""
from __future__ import annotations

from reyn.interfaces.inline.textual_chat.rewind_picker import rewind_row_text


def test_anchor_present_appends_a_fourth_column() -> None:
    """Tier 1: a row carrying a non-empty anchor renders it as the 4th
    ``·``-separated column, after seq/ts/kind."""
    row = rewind_row_text({
        "seq": 1234, "ts": "12:03", "kind": "turn",
        "anchor": "sleep のたびにトークン消費しないで…",
    })
    assert row == "seq 1234 · 12:03 · turn · 「sleep のたびにトークン消費しないで…」"


def test_anchor_absent_omits_the_fourth_column() -> None:
    """Tier 1: deny — a row with no anchor (``""`` or missing key, the
    #1547 no-anchor degrade for a plan-step cut) renders only 3 columns,
    same as before this PR — never a fabricated 4th one."""
    row_missing = rewind_row_text({"seq": 5, "ts": "09:00", "kind": "plan-step"})
    row_empty = rewind_row_text({"seq": 5, "ts": "09:00", "kind": "plan-step", "anchor": ""})
    assert row_missing == "seq 5 · 09:00 · plan-step"
    assert row_empty == "seq 5 · 09:00 · plan-step"


def test_row_never_re_truncates_an_already_truncated_anchor() -> None:
    """Tier 1: deny — `AnchorStore.truncate_anchor` already cuts an anchor
    to (default) 80 characters upstream, at capture time; `rewind_row_text`
    must render whatever string it is handed VERBATIM, never re-slicing it
    (unlike the `ts` column, which this function DOES truncate itself,
    ``_TS_MAX`` — a deliberately different contract for a field this
    function does not own the truncation policy for)."""
    already_long = "x" * 200  # deliberately over 80 — simulates an anchor
    # a caller passed WITHOUT going through truncate_anchor first (a stale
    # anchor-store entry, or a future producer that forgets to truncate).
    row = rewind_row_text({"seq": 1, "kind": "turn", "anchor": already_long})
    assert f"「{already_long}」" in row, (
        "the row must carry the anchor text unmodified — truncation is "
        "truncate_anchor's own job, at capture time, never the row's"
    )
