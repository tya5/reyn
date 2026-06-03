"""Tier 2: OS invariant — phase force-close activation + shared reserves (#1092 C2).

C2 makes the layer-1 force-close trigger LIVE on the phase axis (so PR-D/E can be
built + verified on a real phase force-close, not blind). It pins:

- the SHARED reserve helper builds a TurnBudgetEngine with offload_cap = the
  per-result offload ceiling (#1093 MAX_OFFLOADED_INLINE_BYTES, in tokens) and
  output_reserve = the shared default — built once so chat/plan reuse it in PR-F;
- CompactionEngine exposes its RESOLVED model (#1172) so the phase TurnBudgetEngine
  budgets against the same model, not the cosmetic run-loop router_model;
- PhaseRouterLoopHost.should_force_close fires when the accumulated non-system
  content reaches the threshold, is inert without an engine, and excludes the
  system turn from the content measure.

No mocks: real engines + a real ModelResolver + a real PhaseRouterLoopHost.
"""
from __future__ import annotations

import pytest

from reyn.context_builder import MAX_OFFLOADED_INLINE_BYTES
from reyn.kernel.phase_router_host import PhaseRouterLoopHost
from reyn.llm.model_resolver import ModelResolver
from reyn.services.compaction.engine import estimate_tokens
from reyn.services.turn_budget import (
    DEFAULT_WRAP_UP_OUTPUT_RESERVE_TOKENS,
    TurnBudgetEngine,
    build_default_turn_budget_engine,
)
from tests.test_router_loop import FakeEventLog

_MODEL = "gpt-4o-mini"


# ── shared reserve helper ────────────────────────────────────────────────────


def test_shared_helper_uses_offload_ceiling_and_default_output_reserve() -> None:
    """Tier 2: build_default_turn_budget_engine sources offload_cap from the
    #1093 inline ceiling (in tokens) and output_reserve from the shared default."""
    eng = build_default_turn_budget_engine(_MODEL, use_chars4=True)
    expected_offload = estimate_tokens(
        "x" * MAX_OFFLOADED_INLINE_BYTES, _MODEL, use_chars4=True
    )
    assert eng.budget.offload_cap == expected_offload
    assert eng.budget.output_reserve == DEFAULT_WRAP_UP_OUTPUT_RESERVE_TOKENS
    # threshold still obeys the §5 formula.
    b = eng.budget
    assert b.force_close_threshold == (
        b.max_input - b.T_wrap_SP - b.output_reserve - b.offload_cap
    )


# ── CompactionEngine resolved-model accessor (#1172) ─────────────────────────


def test_compaction_engine_exposes_resolved_model() -> None:
    """Tier 2: CompactionEngine.model returns the RESOLVED LiteLLM string (not the
    class) — the source the phase TurnBudgetEngine budgets against."""
    from reyn.services.compaction.engine import CompactionEngine

    resolver = ModelResolver({"myclass": "openai/resolved-model"})
    eng = CompactionEngine("myclass", events=FakeEventLog(), resolver=resolver)
    assert eng.model == "openai/resolved-model"  # resolved
    assert eng.model != "myclass"                # never the raw class (#1172)


# ── phase host should_force_close ────────────────────────────────────────────


def _host(turn_budget_engine) -> PhaseRouterLoopHost:
    return PhaseRouterLoopHost(
        control_ir_executor=None,
        events=FakeEventLog(),
        phase="p",
        decl=None,
        allowed_ops=None,
        default_sandbox_policy=None,
        agent_name="a",
        agent_role="r",
        output_language="en",
        resolve_model_fn=lambda n: n,
        turn_budget_engine=turn_budget_engine,
    )


def _small_threshold_engine(threshold_tokens: int) -> TurnBudgetEngine:
    """A TurnBudgetEngine whose threshold is a small testable value (large
    reserves pull it down), so a small message can cross it."""
    eng = build_default_turn_budget_engine(_MODEL, use_chars4=True)
    t_max = eng.budget.max_input
    t_wrap = eng.budget.T_wrap_SP
    # threshold = t_max - t_wrap - output_reserve - offload_cap
    offload_cap = 1
    output_reserve = t_max - t_wrap - offload_cap - threshold_tokens
    return TurnBudgetEngine(
        _MODEL, output_reserve=output_reserve, offload_cap=offload_cap,
        use_chars4=True,
    )


@pytest.mark.asyncio
async def test_phase_host_fires_above_threshold_inert_without_engine() -> None:
    """Tier 2: should_force_close fires when non-system content ≥ threshold, and
    a host without an engine is inert (False)."""
    eng = _small_threshold_engine(threshold_tokens=50)
    host = _host(eng)
    # content_tokens ≈ chars/4 (use_chars4). Build a user turn above/below 50 tok.
    above = [{"role": "user", "content": "x" * (60 * 4)}]   # ~60 tok ≥ 50
    below = [{"role": "user", "content": "x" * (40 * 4)}]   # ~40 tok < 50
    assert await host.should_force_close(above, model="p") is True
    assert await host.should_force_close(below, model="p") is False
    # inert without an engine.
    assert await _host(None).should_force_close(above, model="p") is False


@pytest.mark.asyncio
async def test_phase_host_excludes_system_turn_from_content() -> None:
    """Tier 2: the system turn is excluded from the content measure (the wrap-up
    SP swaps it at force-close time). A huge system turn alone does not trip it."""
    eng = _small_threshold_engine(threshold_tokens=50)
    host = _host(eng)
    # A large SYSTEM turn + a tiny user turn → content (user only) stays under.
    msgs = [
        {"role": "system", "content": "x" * (10_000 * 4)},  # excluded
        {"role": "user", "content": "x" * (10 * 4)},        # ~10 tok < 50
    ]
    assert await host.should_force_close(msgs, model="p") is False
