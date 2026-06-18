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

from typing import Any

import pytest

from reyn.core.context_builder import MAX_OFFLOADED_INLINE_BYTES
from reyn.core.kernel.phase_router_host import PhaseRouterLoopHost
from reyn.llm.llm import LLMToolCallResult
from reyn.llm.model_resolver import ModelResolver
from reyn.llm.pricing import TokenUsage
from reyn.runtime.router_loop import RouterLoop
from reyn.services.compaction.engine import estimate_tokens, estimate_tokens_for_turn
from reyn.services.turn_budget import (
    DEFAULT_WRAP_UP_OUTPUT_RESERVE_TOKENS,
    TurnBudgetEngine,
    build_default_turn_budget_engine,
)
from tests.test_router_loop import FakeEventLog, FakeRouterHost

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


def _phase_engine() -> TurnBudgetEngine:
    """A REAL, non-degenerate engine (gpt-3.5-turbo, threshold ~9968 tok). PR-E's
    by-construction assert (threshold > output_reserve + offload_cap) forbids the
    old large-reserve low-threshold trick, so firing tests use the real threshold
    + threshold-sized content (the D2 integration pattern)."""
    return build_default_turn_budget_engine("gpt-3.5-turbo", use_chars4=True)


@pytest.mark.asyncio
async def test_phase_host_fires_above_threshold_inert_without_engine() -> None:
    """Tier 2: should_force_close fires when non-system content ≥ threshold, and
    a host without an engine is inert (False)."""
    eng = _phase_engine()
    t = eng.budget.force_close_threshold
    host = _host(eng)
    # content_tokens ≈ chars/4 (use_chars4). Size a user turn just above/below t.
    above = [{"role": "user", "content": "x" * ((t + 500) * 4)}]   # > t tok
    below = [{"role": "user", "content": "x" * ((t - 500) * 4)}]   # < t tok
    assert await host.should_force_close(above, model="p") is True
    assert await host.should_force_close(below, model="p") is False
    # inert without an engine.
    assert await _host(None).should_force_close(above, model="p") is False


@pytest.mark.asyncio
async def test_phase_host_excludes_system_turn_from_content() -> None:
    """Tier 2: the system turn is excluded from the content measure (the wrap-up
    SP swaps it at force-close time). A huge system turn alone does not trip it."""
    eng = _phase_engine()
    t = eng.budget.force_close_threshold
    host = _host(eng)
    # A large SYSTEM turn (well over threshold) + a tiny user turn → content
    # (user only) stays under, so it does NOT trip.
    msgs = [
        {"role": "system", "content": "x" * ((t + 2000) * 4)},  # excluded
        {"role": "user", "content": "x" * (100 * 4)},           # ~100 tok < t
    ]
    assert await host.should_force_close(msgs, model="p") is False


# ── live path: over-threshold content actually fires force-close (D/E base) ───


class _ThresholdHost(FakeRouterHost):
    """FakeRouterHost whose should_force_close is backed by a REAL small-threshold
    TurnBudgetEngine (the same non-system content measure PhaseRouterLoopHost
    uses) — to exercise the trigger→force-close path end-to-end through run_loop."""

    def __init__(self, engine: TurnBudgetEngine, **kw: Any) -> None:
        super().__init__(**kw)
        self._engine = engine

    async def should_force_close(self, messages: list[dict], *, model: str) -> bool:
        content = sum(
            estimate_tokens_for_turn(m, model, use_chars4=True)
            for m in messages
            if isinstance(m, dict) and m.get("role") != "system"
        )
        return self._engine.should_force_close(content)


class _CapturingLLM:
    def __init__(self) -> None:
        self.last_kwargs: dict = {}

    async def __call__(self, **kwargs: Any) -> LLMToolCallResult:
        self.last_kwargs = kwargs
        return LLMToolCallResult(
            content="consolidated", tool_calls=[], finish_reason="stop",
            usage=TokenUsage(prompt_tokens=10, completion_tokens=2),
        )


@pytest.mark.asyncio
async def test_over_threshold_content_actually_fires_force_close_via_run_loop() -> None:
    """Tier 2: (★live path — the D/E foundation) injecting over-threshold content
    drives the REAL threshold-backed trigger so run_loop SWAPS to the force-close
    call (tools=[], trace_caller=router_force_close). Confirms force-close actually
    FIRES end-to-end, not just should_force_close=True in isolation."""
    eng = _phase_engine()
    t = eng.budget.force_close_threshold
    llm = _CapturingLLM()
    loop = RouterLoop(
        host=_ThresholdHost(eng), chain_id="c2-live", max_iterations=3,
        llm_caller=llm,
    )
    await loop.run("x" * ((t + 500) * 4), [])  # over-threshold user turn → fires
    assert llm.last_kwargs["trace_caller"] == "router_force_close"
    assert llm.last_kwargs["tools"] == []
