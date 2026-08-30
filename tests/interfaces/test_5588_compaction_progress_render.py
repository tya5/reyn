"""Tier 1: #5588 — the shrink-flow progress display's pure render layer.

``compaction_progress_lines``/``compaction_failure_text`` are pure functions
over a plain dataclass — no Session, no Textual app, no event log. Real
``RetryLoopTerminal`` members throughout (the actual production enum, never
a stand-in), since the failure-message mapping's whole point is fidelity to
those exact members.
"""
from __future__ import annotations

from reyn.interfaces.inline.textual_chat.compaction_progress import (
    CompactionProgressSnapshot,
    compaction_failure_text,
    compaction_progress_lines,
)
from reyn.services.compaction.engine import RetryLoopTerminal


def test_not_compacting_renders_nothing():
    """Tier 1: accept/deny pair, deny side — is_compacting=False (the
    default) renders zero lines, regardless of what other fields are set.
    This is the acceptance criterion's own deny half: the display must
    never persist once a pass has ended."""
    snap = CompactionProgressSnapshot(is_compacting=False, spill_done=5, spill_total=12)
    assert compaction_progress_lines(snap) == []


def test_compacting_with_only_the_flag_still_renders_line_1():
    """Tier 1: accept side — is_compacting=True alone (no producer fields
    yet) still renders a correct, honest line 1. This is the #5588
    skeleton-first shape: nothing here is fabricated, but the one real
    signal available renders something true."""
    snap = CompactionProgressSnapshot(is_compacting=True)
    lines = compaction_progress_lines(snap)
    assert lines == ["⟳ 文脈を縮めています（自動で終わります）"]


def test_line_2_names_summary_wait_with_elapsed():
    """Tier 1: waiting_for='summary' renders the 要約 (summary) wait line,
    with the elapsed seconds appended when known."""
    snap = CompactionProgressSnapshot(
        is_compacting=True, waiting_for="summary", waiting_elapsed_s=12.7,
    )
    lines = compaction_progress_lines(snap)
    assert lines[1] == "  要約の応答を待っています 12秒"


def test_line_2_names_main_call_wait_without_elapsed_when_unknown():
    """Tier 1: waiting_for='main_call' renders the 本文 (main body) wait
    line; waiting_elapsed_s=None omits the clock entirely rather than
    coercing to 0秒 (mirrors activity_row.py's own "never fabricate an
    unknown elapsed time" rule)."""
    snap = CompactionProgressSnapshot(is_compacting=True, waiting_for="main_call")
    lines = compaction_progress_lines(snap)
    assert lines[1] == "  本文の応答を待っています"


def test_line_2_absent_when_waiting_for_is_none():
    """Tier 1: waiting_for=None (the default) — no line 2 at all, never a
    fabricated "no progress" line (this render layer never claims to know
    a stall occurred; see the module's own docstring)."""
    snap = CompactionProgressSnapshot(is_compacting=True)
    lines = compaction_progress_lines(snap)
    assert lines == ["⟳ 文脈を縮めています（自動で終わります）"]


def test_line_3_shows_spill_and_call_count_together():
    """Tier 1: #5588 architect correction — spill_done/spill_total and
    call_count must render TOGETHER on the same segment, never one without
    the other (the exact incident this display exists to make visible:
    candidates shrinking while calls grow disproportionately)."""
    snap = CompactionProgressSnapshot(
        is_compacting=True, spill_done=5, spill_total=2469, call_count=43,
    )
    lines = compaction_progress_lines(snap)
    assert "① 退避 5/2469  呼び出し 43" in lines[-1]


def test_line_3_spill_without_call_count_omits_the_call_segment():
    """Tier 1: call_count=None (not yet known/wired) — the spill fraction
    still renders alone, never a fabricated call count."""
    snap = CompactionProgressSnapshot(is_compacting=True, spill_done=5, spill_total=2469)
    lines = compaction_progress_lines(snap)
    assert "① 退避 5/2469" in lines[-1]
    assert "呼び出し" not in lines[-1]


def test_line_3_all_four_rungs_plus_lap_plus_active_marker():
    """Tier 1: the full architect-designed line 3 shape, all fields
    present — matches the issue's own verbatim mockup byte-for-byte on the
    rung segments (spacing choices are this module's own, per lead-coder's
    delegation)."""
    snap = CompactionProgressSnapshot(
        is_compacting=True,
        spill_done=5, spill_total=2469, call_count=43,
        slice_len=4,
        head_available=True, tail_available=True,
        budget_halvings_done=1, budget_halvings_max=4,
        lap=2, active_rung="spill",
    )
    lines = compaction_progress_lines(snap)
    line3 = lines[-1]
    assert "① 退避 5/2469  呼び出し 43" in line3
    assert "② 分割 4" in line3
    assert "③ 補充 head/tail" in line3
    assert "④ 予算 1/4" in line3
    assert "周回 2" in line3
    assert "← 今 ①" in line3


def test_line_3_refill_shows_only_the_still_available_side():
    """Tier 1: head_available=True, tail_available=False renders only
    'head' — never fabricates the unavailable side as present."""
    snap = CompactionProgressSnapshot(
        is_compacting=True, head_available=True, tail_available=False,
    )
    lines = compaction_progress_lines(snap)
    assert "③ 補充 head" in lines[-1]
    assert "tail" not in lines[-1]


def test_line_3_absent_when_no_rung_field_is_known():
    """Tier 1: every rung field None (skeleton-only state) — line 3 is
    simply absent, not an empty/placeholder line."""
    snap = CompactionProgressSnapshot(is_compacting=True, waiting_for="summary")
    lines = compaction_progress_lines(snap)
    assert lines == [
        "⟳ 文脈を縮めています（自動で終わります）",
        "  要約の応答を待っています",
    ]


def test_failure_text_mid_floor():
    """Tier 1: MID_FLOOR maps to architect's own exact wording — never a
    parse of UnrecoveredError's own reason/repr() text."""
    assert compaction_failure_text(RetryLoopTerminal.MID_FLOOR) == (
        "1つのやり取りが単独で大きすぎます"
    )


def test_failure_text_room_floor():
    """Tier 1: ROOM_FLOOR maps to architect's own exact wording, distinct
    from MID_FLOOR's — the issue's own deny criterion (the two members
    must never collapse to the same text)."""
    assert compaction_failure_text(RetryLoopTerminal.ROOM_FLOOR) == (
        "最新のメッセージだけで窓に入りません"
    )


def test_failure_text_mid_floor_and_room_floor_are_distinct():
    """Tier 1: #5588 deny criterion, explicit — the 2 RetryLoopTerminal
    members must render as 2 different strings, not collapsed to one."""
    assert compaction_failure_text(RetryLoopTerminal.MID_FLOOR) != (
        compaction_failure_text(RetryLoopTerminal.ROOM_FLOOR)
    )
