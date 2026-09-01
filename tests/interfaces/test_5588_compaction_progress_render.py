"""Tier 1: #5588 — the shrink-flow progress display's pure render layer.

``compaction_progress_lines``/``compaction_failure_text`` are pure functions
over a plain dataclass — no Session, no Textual app, no event log. Real
``RetryLoopTerminal`` members throughout (the actual production enum, never
a stand-in), since the failure-message mapping's whole point is fidelity to
those exact members.
"""
from __future__ import annotations

from reyn.interfaces.inline.textual_chat.chrome import ctx_pane_lines
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


# ── the Ctx pane's ``folded`` row (#5578's persisted watermark) ─────────
#
# Three states that must stay distinct: two of them arrive as ``None`` at
# the call site and would otherwise collapse into the same lying ``None``
# this pane's own #5009 pass exists to prevent.


def _folded(snap: dict) -> str:
    (line,) = [ln for ln in ctx_pane_lines(snap) if ln.startswith("folded")]
    return line


def test_folded_row_says_not_reported_when_the_key_is_absent():
    """Tier 1: REMOTE/AG-UI does not project compaction_progress_raw at all
    (#5605 tracks closing that), so the key is ABSENT — which must read as
    "not reported", the same words the compaction row above uses for its
    own version of this state, never as "no fold yet"."""
    assert "not reported" in _folded({"ctx_window": 1000, "ctx_used": 100})


def test_folded_row_distinguishes_no_fold_yet_from_not_reported():
    """Tier 1: the load-bearing distinction. LOCAL genuinely reports the
    dict; a session that never overflowed simply has no persisted fold, and
    that is a MEASURED "none", not an unreported one. A pane that printed
    the same words for both would tell an operator on a working local
    session that their own client cannot see the figure."""
    snap = {
        "ctx_window": 1000, "ctx_used": 100,
        "compaction_progress_raw": {"persisted_covers_through_seq": None},
    }
    line = _folded(snap)
    assert "no recovery fold persisted yet" in line, line
    assert "not reported" not in line, line


def test_folded_row_shows_the_real_seq_when_one_was_persisted():
    """Tier 1: accept side — a real watermark renders as the seq the event
    carried, thousands-separated like every other figure in this pane."""
    snap = {
        "ctx_window": 1000, "ctx_used": 100,
        "compaction_progress_raw": {"persisted_covers_through_seq": 2469},
    }
    line = _folded(snap)
    assert "through seq 2,469" in line, line
    assert "not reported" not in line, line


def test_folded_row_does_not_depend_on_is_compacting():
    """Tier 2: #5618 independence. The shrink-progress ROW (a different
    surface, `compaction_progress_lines`) gates on ``is_compacting``, and
    owner's real machine found that gate never rises during retry-ladder
    recovery — ``CompactionController._compacting`` is set only inside
    ``force_compact_now``, while the ladder calls the engine directly
    (#5618, architect designing the real signal).

    This row must not share that fate, and does not by construction: it
    reads the persisted watermark, which the ladder's OWN success path
    emits (``on_summary_used`` -> ``persist_recovery_summary`` ->
    ``recovery_summary_persisted``). Pinned rather than assumed — with
    ``is_compacting`` False (the exact state #5618 reports), the real seq
    still renders. This test does NOT claim to fix #5618: that is the
    in-flight progress row, a different question (result vs progress)."""
    snap = {
        "ctx_window": 1000, "ctx_used": 100,
        "compaction_progress_raw": {
            "is_compacting": False,
            "persisted_covers_through_seq": 2469,
        },
    }
    assert "through seq 2,469" in _folded(snap)


def test_folded_row_never_calls_the_expensive_compaction_status_fn():
    """Tier 2: the whole point of this row — it reads the cached figure,
    never ``Session.context_window_status()``. That function is a
    json.dumps + token-estimate of the full router-view history, which is
    why ``_snapshot()`` stores it UNCALLED and app.py builds only the ONE
    open pane on frame arrival ("load-bearing, not an optimization", its
    own docstring). A future refactor that reached for the status_fn to
    fill this row would reinstate exactly that cost; this test fails if it
    does, by handing over a status_fn that records being called.

    Scoped to what this row needs: the pane's OWN pre-existing
    ``compaction`` row legitimately calls status_fn once, so the assertion
    is that removing this row's inputs changes nothing about that count —
    i.e. the folded row adds ZERO calls, not that the pane makes none."""
    calls: list[int] = []

    def _status_fn():
        calls.append(1)
        return {"effective_trigger": 100, "free_window": 40}

    base = {
        "ctx_window": 1000, "ctx_used": 100,
        "ctx_compaction_status_fn": _status_fn,
        "ctx_compaction_reported": True,
    }
    ctx_pane_lines(dict(base))
    without_folded_input = len(calls)

    calls.clear()
    ctx_pane_lines({**base, "compaction_progress_raw": {
        "persisted_covers_through_seq": 2469,
    }})
    assert len(calls) == without_folded_input, (
        f"the folded row must add no status_fn calls; base made "
        f"{without_folded_input}, with the row it made {len(calls)}"
    )
