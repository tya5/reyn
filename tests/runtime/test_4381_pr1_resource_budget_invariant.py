"""Tier 2: #4381 PR-1 — the resource/budget invariant `resolve_effective_trigger_and_budgets`
now enforces: "a result that passed the resource boundary must not exceed the
budget boundary after conversion."

Real collaborators throughout: `compute_budgets` (the same function
`CompactionEngine` itself uses) builds a real `ComputedBudgets`; only the
tiny attribute-passthrough container matching `compaction_controller._engine
.budgets`'s shape is hand-built (no logic of its own — mirrors the existing
`_FakeEngine`/`_FakeController` idiom in
`tests/llm/test_context_budget_advisor_raw_window.py`), and `EventLog` +
`tests._support.events.collect_events` capture the real subscriber path.
"""
from __future__ import annotations

from reyn.config import CompactionConfig
from reyn.core.context_builder import INLINE_CAP_CHARS_PER_TOKEN, control_ir_inline_cap
from reyn.core.events.events import EventLog
from reyn.llm.model_budget import get_max_input_tokens
from reyn.runtime.services import router_history_buffer as rhb
from reyn.services.compaction.engine import compute_budgets
from tests._support.events import collect_events

_MODEL = "openai/gpt-4o"


class _Engine:
    def __init__(self, budgets) -> None:
        self.budgets = budgets


class _Controller:
    def __init__(self, engine) -> None:
        self._engine = engine


def _budgets_with_effective_trigger(t_sp: int):
    return compute_budgets(CompactionConfig(), _MODEL, T_SP=t_sp, T_comp_SP=500)


def _reset_warned():
    rhb._resource_budget_warned.clear()


def test_resource_bound_actually_exceeds_the_small_budget_in_this_scenario():
    """Tier 2: sanity precondition for the tests below — with `T_SP` close to
    the model's window, `effective_trigger` collapses well under the
    resource boundary's own token-equivalent, so the invariant genuinely
    trips (not an assumption)."""
    resource_bound_chars = control_ir_inline_cap(_MODEL)
    resource_bound_tokens = -(-resource_bound_chars // INLINE_CAP_CHARS_PER_TOKEN)
    small_budgets = _budgets_with_effective_trigger(get_max_input_tokens(_MODEL) - 500)
    assert small_budgets.effective_trigger < resource_bound_tokens


def test_violation_emits_resource_cap_exceeds_budget_trigger_once():
    """Tier 2: a genuine violation (resource bound, converted to tokens,
    exceeds the budget trigger) emits `resource_cap_exceeds_budget_trigger`
    exactly once — repeated calls with the SAME (model, phase) do not
    re-warn (the SSoT is called on every trigger resolution)."""
    _reset_warned()
    events = EventLog()
    collected = collect_events(events)
    budgets = _budgets_with_effective_trigger(get_max_input_tokens(_MODEL) - 500)
    controller = _Controller(_Engine(budgets))

    for _ in range(3):
        result = rhb.resolve_effective_trigger_and_budgets(controller, _MODEL, events)
        assert result == (budgets.effective_trigger, budgets.head_budget, budgets.tail_budget)

    warnings = [e for e in collected if e.type == "resource_cap_exceeds_budget_trigger"]
    (only,) = warnings
    assert only.data["model"] == _MODEL
    assert only.data["phase"] == ""
    assert only.data["effective_trigger"] == budgets.effective_trigger
    assert only.data["resource_bound_tokens"] > budgets.effective_trigger


def test_violation_warns_again_for_a_different_phase_same_model():
    """Tier 2: warn-once granularity is per (model, phase), not per model
    alone — a DIFFERENT phase with the same model re-warns."""
    _reset_warned()
    events = EventLog()
    collected = collect_events(events)
    budgets = _budgets_with_effective_trigger(get_max_input_tokens(_MODEL) - 500)
    controller = _Controller(_Engine(budgets))

    rhb.resolve_effective_trigger_and_budgets(controller, _MODEL, events, phase="alpha")
    rhb.resolve_effective_trigger_and_budgets(controller, _MODEL, events, phase="alpha")
    rhb.resolve_effective_trigger_and_budgets(controller, _MODEL, events, phase="beta")

    warnings = [e for e in collected if e.type == "resource_cap_exceeds_budget_trigger"]
    phases_warned = sorted(e.data["phase"] for e in warnings)
    assert phases_warned == ["alpha", "beta"]


def test_no_violation_emits_nothing():
    """Tier 2: accept-side twin — a normal-sized `T_SP` leaves `effective_trigger`
    comfortably above the resource bound's token-equivalent; no event fires
    at all (not a present-but-benign one)."""
    _reset_warned()
    events = EventLog()
    collected = collect_events(events)
    budgets = _budgets_with_effective_trigger(2_000)
    controller = _Controller(_Engine(budgets))

    result = rhb.resolve_effective_trigger_and_budgets(controller, _MODEL, events)

    assert result == (budgets.effective_trigger, budgets.head_budget, budgets.tail_budget)
    assert not [e for e in collected if e.type == "resource_cap_exceeds_budget_trigger"]


def test_no_events_sink_does_not_raise():
    """Tier 2: `events=None` (many test/estimation-path callers construct
    without one) — the check runs and detects the violation internally but
    never tries to emit, and never raises for lack of a sink."""
    _reset_warned()
    budgets = _budgets_with_effective_trigger(get_max_input_tokens(_MODEL) - 500)
    controller = _Controller(_Engine(budgets))

    result = rhb.resolve_effective_trigger_and_budgets(controller, _MODEL, None)

    assert result == (budgets.effective_trigger, budgets.head_budget, budgets.tail_budget)


def test_fallback_path_no_engine_budgets_still_checks_the_invariant():
    """Tier 2: the `budgets is None` fallback branch (no compaction engine
    attached — the `get_max_input_tokens(model) // 4` fallback trigger) is
    ALSO covered by the invariant check, not just the engine-budgets branch."""
    _reset_warned()
    events = EventLog()
    collected = collect_events(events)

    rhb.resolve_effective_trigger_and_budgets(None, _MODEL, events)

    # Fallback trigger = get_max_input_tokens(model) // 4, comfortably above
    # the resource bound for this model — no violation in the fallback path
    # with a real, reasonably-sized model.
    assert not [e for e in collected if e.type == "resource_cap_exceeds_budget_trigger"]
