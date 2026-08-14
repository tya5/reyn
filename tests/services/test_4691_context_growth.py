"""Tier 2: #4691 Phase A.5 — ``BudgetTracker.last_context_growth()``.

Owner ruling (via lead-coder, issue #4691): the gutter's per-row figure is
the SIGNED delta of ``prompt_tokens`` between the two most recent LLM calls
this session made — not a per-turn total (already displayed on the cost
tab) and not an absolute per-call figure (duplicates ctx tab's own number).
Tracked on ``BudgetTracker`` rather than ``RouterLoop`` because a fresh
``RouterLoop`` is constructed per TURN (``router_loop_driver.py``), so
nothing turn-local could see the PREVIOUS turn's last call to diff
against — ``BudgetTracker`` is the one session-persistent object every
call already passes through (``record_llm``).

Real ``BudgetTracker``/``TokenUsage`` — no mocks.
"""
from __future__ import annotations

from reyn.llm.pricing import TokenUsage
from reyn.runtime.budget.budget import BudgetTracker, CostConfig

_MODEL = "gpt-4o"


def _record(tracker: BudgetTracker, prompt: int) -> None:
    tracker.record_llm(
        model=_MODEL, agent="alpha",
        usage=TokenUsage(prompt_tokens=prompt, completion_tokens=1),
    )


def test_the_first_call_this_session_has_no_baseline_to_diff_against() -> None:
    """Tier 2: no prior call recorded yet — None, never a fabricated 0."""
    tracker = BudgetTracker(CostConfig())
    assert tracker.last_context_growth() is None
    _record(tracker, prompt=1000)
    assert tracker.last_context_growth() is None, (
        "the FIRST call has nothing to diff against — still no baseline "
        "after it, only from the SECOND call onward"
    )


def test_the_second_call_reports_the_signed_delta_from_the_first() -> None:
    """Tier 2: the core contract — growth is prompt_tokens(this call) minus
    prompt_tokens(the immediately preceding call), positive when the
    context grew."""
    tracker = BudgetTracker(CostConfig())
    _record(tracker, prompt=45_808)
    _record(tracker, prompt=45_890)
    assert tracker.last_context_growth() == 82


def test_consecutive_calls_each_report_their_own_delta_not_a_running_total() -> None:
    """Tier 2: a real session's own shape (issue #4691's measured example) —
    each call's growth is against the IMMEDIATELY PRECEDING call, not the
    first call in the sequence (which would make later deltas balloon into
    a running total, reproducing the very "near-zero information" problem
    the signed-delta design replaced)."""
    tracker = BudgetTracker(CostConfig())
    prompts = [45_808, 45_890, 45_984, 46_024, 46_100]
    deltas = []
    for p in prompts:
        _record(tracker, prompt=p)
        deltas.append(tracker.last_context_growth())
    assert deltas == [None, 82, 94, 40, 76]


def test_a_compaction_shrinking_the_context_reports_a_negative_delta() -> None:
    """Tier 2: owner ruling — a compaction between two calls (the next
    call's prompt_tokens smaller than the last) reports a NEGATIVE growth,
    with its sign — the gutter is responsible for keeping the sign
    visible; this tracker just reports the honest signed number."""
    tracker = BudgetTracker(CostConfig())
    _record(tracker, prompt=138_000)
    _record(tracker, prompt=18_000)  # a compaction just ran
    assert tracker.last_context_growth() == -120_000


def test_a_call_with_no_turn_in_scope_still_updates_the_growth_baseline() -> None:
    """Tier 2: growth tracking is unconditional — record_llm's existing
    per-turn attribution is guarded on chain_id being non-None (a call with
    no turn in scope skips ITS bucket), but context growth is a SESSION-wide
    concept independent of turn attribution, so it updates regardless."""
    tracker = BudgetTracker(CostConfig())
    tracker.record_llm(model=_MODEL, agent=None, usage=TokenUsage(prompt_tokens=1000), chain_id=None)
    tracker.record_llm(model=_MODEL, agent=None, usage=TokenUsage(prompt_tokens=1100), chain_id=None)
    assert tracker.last_context_growth() == 100
