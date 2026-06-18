"""Tier 2: OS invariant — plan-axis cumulative-current-turn force-close
(#1285 / #1092 plan axis, PR1 FLOOR).

`_PlanStepHost` now implements the phase-style force-close host interface
(should_force_close / record_force_close / forced_close_result /
wrap_up_output_reserve) + is threaded a TurnBudgetEngine at construction, so a
single long plan step force-closes (bounded current turn) instead of growing
unbounded. PR1 is the FLOOR (the wrap-up consolidation becomes the step output);
PR2 re-enters the same step from it.

Mirrors the phase activation test (test_force_close_phase_activation_1092):
``build_default_turn_budget_engine("gpt-3.5-turbo")`` gives a small-context
threshold so it can be crossed with crafted content (no 1M-token messages).

No mocks of the engine — a real small-threshold TurnBudgetEngine. No private
state asserted beyond the documented host interface.
"""
from __future__ import annotations

import asyncio

from reyn.runtime.planner import Plan, PlanStep, _PlanStepHost
from reyn.services.turn_budget import (
    assert_turn_budget_bounds,
    build_default_turn_budget_engine,
)

_SMALL_MODEL = "gpt-3.5-turbo"  # small max_input → small, crossable threshold


def _engine():
    return build_default_turn_budget_engine(_SMALL_MODEL, use_chars4=True)


def _host(engine):
    step = PlanStep(id="s1", description="do the thing", tools=())
    plan = Plan(goal="g", steps=(step,))
    # parent is stored but untouched by the force-close interface.
    return _PlanStepHost(
        plan=plan, step=step, prior_results={}, parent=object(),
        turn_budget_engine=engine,
    )


def _content_msgs(total_tokens_target: int) -> list[dict]:
    # use_chars4 ⇒ ~4 chars/token; build a user turn whose content crosses target.
    return [{"role": "user", "content": "x" * (total_tokens_target * 4)}]


def test_should_force_close_fires_above_threshold_inert_when_engine_none() -> None:
    """Tier 2: fires when non-system content ≥ threshold; engine None → inert (False)."""
    eng = _engine()
    t = eng.budget.force_close_threshold
    host = _host(eng)
    above = _content_msgs(t + 50)
    below = _content_msgs(max(t - 50, 0))
    assert asyncio.run(host.should_force_close(above, model="p")) is True
    assert asyncio.run(host.should_force_close(below, model="p")) is False
    # Engine absent (not activated) → always False = byte-identical to pre-#1285.
    assert asyncio.run(_host(None).should_force_close(above, model="p")) is False


def test_should_force_close_excludes_system_turns() -> None:
    """Tier 2: the system turn is excluded from the content measure (the wrap-up
    SP swaps it at force-close time), so a huge system turn alone does not fire."""
    eng = _engine()
    t = eng.budget.force_close_threshold
    host = _host(eng)
    msgs = [{"role": "system", "content": "x" * ((t + 100) * 4)},
            {"role": "user", "content": "small"}]
    assert asyncio.run(host.should_force_close(msgs, model="p")) is False


def test_wrap_up_output_reserve_caps_consolidation() -> None:
    """Tier 2: wrap_up_output_reserve = engine output_reserve (the wrap-up max_tokens
    hard-cap), None when no engine."""
    eng = _engine()
    assert _host(eng).wrap_up_output_reserve == eng.budget.output_reserve
    assert _host(None).wrap_up_output_reserve is None


def test_record_force_close_roundtrip() -> None:
    """Tier 2: record_force_close stores the consolidation finish for the planner
    to read as the step output (FLOOR); None before any force-close."""
    host = _host(_engine())
    assert host.forced_close_result is None
    sentinel = object()
    host.record_force_close(sentinel)
    assert host.forced_close_result is sentinel


def test_by_construction_termination_bound_holds() -> None:
    """Tier 2: the plan engine satisfies assert_turn_budget_bounds
    (force_close_threshold > output_reserve + offload_cap) so each re-entry makes
    progress — finite re-entries (the #1092 by-construction invariant). Raises if
    violated."""
    assert_turn_budget_bounds(_engine().budget)  # must not raise


def test_force_close_orthogonal_to_fp0031_retry() -> None:
    """Tier 2: the force-close trigger is purely content-driven and shares NO state
    with FP-0031-C/D transient-failure retry — they are distinct triggers that must
    not conflate (force-close = cumulative-context; FP-0031 = exception-driven).

    should_force_close takes only (messages, model) — no attempt/failure input —
    so it is idempotent across calls (a retry loop re-invoking the step cannot
    flip the force-close verdict for the same content), and the engine is built
    once per step independent of the planner's retry budget.
    """
    eng = _engine()
    host = _host(eng)
    above = _content_msgs(eng.budget.force_close_threshold + 50)
    # Idempotent: same content → same verdict regardless of how many times the
    # planner's retry loop might re-enter (no internal attempt/failure state).
    verdicts = [asyncio.run(host.should_force_close(above, model="p")) for _ in range(3)]
    assert verdicts == [True, True, True]
    # And the force-close interface exposes no retry/attempt/failure coupling.
    for retry_attr in ("attempt", "retry", "last_exc", "step_retry_limit", "failure"):
        assert not hasattr(host, retry_attr), (
            f"_PlanStepHost force-close must not couple to FP-0031 retry state "
            f"(found {retry_attr!r})"
        )
