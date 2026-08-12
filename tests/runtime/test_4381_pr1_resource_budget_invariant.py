"""Tier 2: #4381 PR-1 — the resource/budget invariant `resolve_effective_trigger_and_budgets`
now enforces: "a result that passed the resource boundary must not exceed the
budget boundary after conversion."

#4381 PR-5 update: the resource boundary moved from a window-derived,
model-dependent CHAR count to a fixed, model-INDEPENDENT config BYTE value
(`ReadCapConfig.inline_bytes`) — so a "genuine violation" scenario can no
longer be engineered by picking a model with a huge window (the old
approach); every test below builds an EXPLICIT small/large `ReadCapConfig`
and threads it in via `read_cap_config=`, rather than pinning the shipped
default's exact number (owner/architect: the default value itself is a
separate, still-open decision — these tests must survive it changing).

Real collaborators throughout: `compute_budgets` (the same function
`CompactionEngine` itself uses) builds a real `ComputedBudgets`; only the
tiny attribute-passthrough container matching `compaction_controller._engine
.budgets`'s shape is hand-built (no logic of its own — mirrors the existing
`_FakeEngine`/`_FakeController` idiom in
`tests/llm/test_context_budget_advisor_raw_window.py`), and `EventLog` +
`tests._support.events.collect_events` capture the real subscriber path.
"""
from __future__ import annotations

from reyn.config import CompactionConfig, ReadCapConfig
from reyn.core.context_builder import INLINE_CAP_BYTES_PER_TOKEN, control_ir_inline_cap
from reyn.core.events.events import EventLog
from reyn.runtime.services import router_history_buffer as rhb
from reyn.services.compaction.engine import compute_budgets
from tests._support.events import collect_events

_MODEL = "openai/gpt-4o"
# A HUGE resource bound (bytes) whose token-equivalent deliberately EXCEEDS
# any reasonable effective_trigger — the invariant is "resource bound must
# not exceed budget after conversion", so a huge resource bound is what
# TRIPS it (a result that fits under this cap can still overflow a smaller
# model budget). Makes a "genuine violation" scenario trivial to construct
# without depending on the shipped default's exact number.
_HUGE_CAP = ReadCapConfig(inline_bytes=10_000_000)  # -> 2_500_000 tokens
# A tiny resource bound whose token-equivalent is comfortably UNDER any
# reasonable effective_trigger — the accept-side "no violation" case.
_TINY_CAP = ReadCapConfig(inline_bytes=40)  # -> ceil(40/4) = 10 tokens


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
    """Tier 2: sanity precondition for the tests below — with a HUGE
    ``ReadCapConfig``, the resource boundary's own token-equivalent is
    LARGER than a normal ``effective_trigger``, so the invariant genuinely
    trips (not an assumption)."""
    resource_bound_bytes = control_ir_inline_cap(_HUGE_CAP)
    resource_bound_tokens = -(-resource_bound_bytes // INLINE_CAP_BYTES_PER_TOKEN)
    normal_budgets = _budgets_with_effective_trigger(2_000)
    assert normal_budgets.effective_trigger < resource_bound_tokens


def test_violation_emits_resource_cap_exceeds_budget_trigger_once():
    """Tier 2: a genuine violation (resource bound, converted to tokens,
    exceeds the budget trigger) emits `resource_cap_exceeds_budget_trigger`
    exactly once — repeated calls with the SAME (model, phase) do not
    re-warn (the SSoT is called on every trigger resolution)."""
    _reset_warned()
    events = EventLog()
    collected = collect_events(events)
    budgets = _budgets_with_effective_trigger(2_000)
    controller = _Controller(_Engine(budgets))

    for _ in range(3):
        result = rhb.resolve_effective_trigger_and_budgets(
            controller, _MODEL, events, read_cap_config=_HUGE_CAP,
        )
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
    budgets = _budgets_with_effective_trigger(2_000)
    controller = _Controller(_Engine(budgets))

    rhb.resolve_effective_trigger_and_budgets(
        controller, _MODEL, events, phase="alpha", read_cap_config=_HUGE_CAP,
    )
    rhb.resolve_effective_trigger_and_budgets(
        controller, _MODEL, events, phase="alpha", read_cap_config=_HUGE_CAP,
    )
    rhb.resolve_effective_trigger_and_budgets(
        controller, _MODEL, events, phase="beta", read_cap_config=_HUGE_CAP,
    )

    warnings = [e for e in collected if e.type == "resource_cap_exceeds_budget_trigger"]
    phases_warned = sorted(e.data["phase"] for e in warnings)
    assert phases_warned == ["alpha", "beta"]


def test_no_violation_emits_nothing():
    """Tier 2: accept-side twin — a TINY resource bound leaves
    `effective_trigger` comfortably ABOVE the resource bound's
    token-equivalent; no event fires at all (not a present-but-benign
    one)."""
    _reset_warned()
    events = EventLog()
    collected = collect_events(events)
    budgets = _budgets_with_effective_trigger(2_000)
    controller = _Controller(_Engine(budgets))

    result = rhb.resolve_effective_trigger_and_budgets(
        controller, _MODEL, events, read_cap_config=_TINY_CAP,
    )

    assert result == (budgets.effective_trigger, budgets.head_budget, budgets.tail_budget)
    assert not [e for e in collected if e.type == "resource_cap_exceeds_budget_trigger"]


def test_no_read_cap_config_falls_back_to_the_shipped_default():
    """Tier 2: accept-side — omitting ``read_cap_config`` entirely (a
    caller with no config threaded, e.g. ``ContextBudgetAdvisor``'s own
    call today) must not raise, and resolves via ``control_ir_inline_cap``'s
    own ``config=None`` default rather than crashing on a missing arg."""
    _reset_warned()
    events = EventLog()
    budgets = _budgets_with_effective_trigger(2_000)
    controller = _Controller(_Engine(budgets))

    result = rhb.resolve_effective_trigger_and_budgets(controller, _MODEL, events)

    assert result == (budgets.effective_trigger, budgets.head_budget, budgets.tail_budget)


def test_no_events_sink_does_not_raise():
    """Tier 2: `events=None` (many test/estimation-path callers construct
    without one) — the check runs and detects the violation internally but
    never tries to emit, and never raises for lack of a sink."""
    _reset_warned()
    budgets = _budgets_with_effective_trigger(2_000)
    controller = _Controller(_Engine(budgets))

    result = rhb.resolve_effective_trigger_and_budgets(
        controller, _MODEL, None, read_cap_config=_HUGE_CAP,
    )

    assert result == (budgets.effective_trigger, budgets.head_budget, budgets.tail_budget)


def test_fallback_path_no_engine_budgets_still_checks_the_invariant():
    """Tier 2: the `budgets is None` fallback branch (no compaction engine
    attached — the `get_max_input_tokens(model) // 4` fallback trigger) is
    ALSO covered by the invariant check, not just the engine-budgets branch."""
    _reset_warned()
    events = EventLog()
    collected = collect_events(events)

    rhb.resolve_effective_trigger_and_budgets(None, _MODEL, events, read_cap_config=_TINY_CAP)

    # Fallback trigger = get_max_input_tokens(model) // 4, comfortably above
    # the TINY resource bound's token-equivalent — no violation in the
    # fallback path with a real, reasonably-sized model.
    assert not [e for e in collected if e.type == "resource_cap_exceeds_budget_trigger"]
