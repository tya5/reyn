"""Tier 2/3a: by-construction termination guarantee (#1092 PR-E, the critical gate).

The force-close re-entry (D2) terminates BY CONSTRUCTION, not by luck:

- the wrap-up consolidation is HARD-CAPPED ≤ output_reserve (via max_tokens on the
  wrap-up call) — so it provably sits below the threshold;
- ``assert_turn_budget_bounds`` enforces ``force_close_threshold > output_reserve +
  offload_cap`` (LOCKED #1092) — so the re-injected checkpoint leaves room for a
  full working increment below the threshold → every re-entry makes a full
  increment of progress → a finite-work phase converges in FEW re-entries, making
  the max_phase_visits abort UNREACHABLE for a well-configured phase. A degenerate
  config is rejected at construction (fail-fast).

The empirical half reuses the D2 force-close firing infra and shows convergence in
a FEW re-entries (the forward-flag: a legitimate large phase does not exhaust the
shared max_phase_visits budget). Real engines + real OSRuntime; no mocks.
"""
from __future__ import annotations

from typing import Any

import pytest

from reyn.chat.router_loop import RouterLoop
from reyn.llm.llm import LLMToolCallResult
from reyn.llm.model_resolver import ModelSpec
from reyn.llm.pricing import TokenUsage
from reyn.services.turn_budget import (
    TurnBudget,
    assert_turn_budget_bounds,
    build_default_turn_budget_engine,
)
from tests.test_router_loop import FakeRouterHost

# ── by-construction assert + progress_margin ─────────────────────────────────


def test_progress_margin_and_assert_reject_no_progress_config() -> None:
    """Tier 2: a config with threshold > 0 but output_reserve + offload_cap ≥
    threshold (no room for a full increment of progress) has progress_margin ≤ 0
    and is REJECTED by assert_turn_budget_bounds — the by-construction gate (the
    old threshold>0 check would have passed this degenerate config)."""
    bad = TurnBudget(
        max_input=1000, T_wrap_SP=100, output_reserve=400, offload_cap=400,
        force_close_threshold=1000 - 100 - 400 - 400,  # = 100 > 0, but...
    )
    assert bad.force_close_threshold == 100       # threshold IS positive
    assert bad.progress_margin == 100 - 400 - 400  # = -700 ≤ 0 (no progress room)
    with pytest.raises(AssertionError):
        assert_turn_budget_bounds(bad)


def test_default_engine_has_positive_progress_margin() -> None:
    """Tier 2: a well-configured (default-reserve) engine has progress_margin > 0
    — the re-entry can make a full increment of progress → termination guaranteed,
    visit-cap-abort unreachable."""
    for model in ("gpt-4o-mini", "gpt-3.5-turbo"):
        eng = build_default_turn_budget_engine(model, use_chars4=True)
        assert eng.budget.progress_margin > 0
        assert_turn_budget_bounds(eng.budget)  # does not raise
        # margin == threshold − output_reserve − offload_cap.
        b = eng.budget
        assert b.progress_margin == (
            b.force_close_threshold - b.output_reserve - b.offload_cap
        )


# ── max_tokens hard-cap on the wrap-up call ──────────────────────────────────


class _ReserveHost(FakeRouterHost):
    """FakeRouterHost exposing wrap_up_output_reserve (= a phase host with a
    force-close engine)."""

    def __init__(self, reserve: int | None, **kw: Any) -> None:
        super().__init__(**kw)
        self._reserve = reserve

    @property
    def wrap_up_output_reserve(self) -> int | None:
        return self._reserve


class _CapturingLLM:
    def __init__(self) -> None:
        self.model: Any = None

    async def __call__(self, **kwargs: Any) -> LLMToolCallResult:
        self.model = kwargs.get("model")
        return LLMToolCallResult(
            content="c", tool_calls=[], finish_reason="stop",
            usage=TokenUsage(prompt_tokens=5, completion_tokens=2),
        )


@pytest.mark.asyncio
async def test_force_close_hard_caps_output_via_max_tokens() -> None:
    """Tier 2: when the host exposes wrap_up_output_reserve, the wrap-up call is
    issued with a ModelSpec carrying max_tokens=output_reserve — hard-capping the
    consolidation ≤ output_reserve by construction."""
    llm = _CapturingLLM()
    loop = RouterLoop(host=_ReserveHost(2048), chain_id="e", max_iterations=3,
                      llm_caller=llm)
    await loop._force_close_call(
        [{"role": "system", "content": "sp"}, {"role": "user", "content": "u"}],
        resolved_model="gpt-3.5-turbo",
    )
    assert isinstance(llm.model, ModelSpec)
    assert llm.model.kwargs.get("max_tokens") == 2048


@pytest.mark.asyncio
async def test_no_reserve_host_does_not_cap() -> None:
    """Tier 2: a host WITHOUT wrap_up_output_reserve (= chat) issues the wrap-up
    call with the bare model string — no cap (chat handoff is PR-F)."""
    llm = _CapturingLLM()
    loop = RouterLoop(host=FakeRouterHost(), chain_id="e", max_iterations=3,
                      llm_caller=llm)
    await loop._force_close_call(
        [{"role": "system", "content": "sp"}, {"role": "user", "content": "u"}],
        resolved_model="gpt-3.5-turbo",
    )
    assert llm.model == "gpt-3.5-turbo"  # bare string, no ModelSpec/max_tokens


# ── empirical: convergence in FEW re-entries (forward-flag) ───────────────────


def test_legitimate_phase_converges_in_few_reentries(tmp_path, monkeypatch) -> None:
    """Tier 3a: (forward-flag) a phase that force-closes once converges to a
    genuine finish in a FEW re-entries — well under the max_phase_visits cap (25),
    so a legitimate large phase does NOT exhaust the shared visit budget. Reuses
    the D2 force-close firing infra (real OSRuntime, real read_file)."""
    import asyncio

    from tests.test_force_close_reentry_integration_1092 import _setup

    rt = _setup(monkeypatch, tmp_path, always_read=False)
    result = asyncio.run(rt.run({"type": "input", "data": {}}))
    types = [e.type for e in rt.events.all()]
    assert "phase_force_close_reentered" in types
    assert result is not None  # converged to a genuine finish
    # FEW re-entries — far below max_phase_visits (25). loop_limit_exceeded must
    # NOT fire (the by-construction floor keeps convergence fast).
    assert types.count("phase_force_close_reentered") <= 3
    assert "loop_limit_exceeded" not in types
