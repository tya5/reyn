"""Tier 2: #1496 plan/phase axes — limit-deny → force-close wrap-up.

Plan axis (site I: step_max_iterations) and phase axis (sites A/F:
max_act_turns / phase_seconds) share RouterLoop.run_loop() and inherit
the limit-deny force-close path from the chat axis (#1497).

These tests verify axis-specific behavior:
- forced_close_result set on host (plan: step output collection;
  phase: checkpoint persistence) when wrap-up produces text
- forced_close_result NOT set when wrap-up produces no text (guard)

_PlanStepHost.put_outbox captures text in _captured_text (not an
outbox list). PhaseRouterLoopHost.put_outbox is a no-op.

No mocks. _PlanStepHost and PhaseRouterLoopHost are real instances.
"""
from __future__ import annotations

import json

import pytest

from reyn.config import OnLimitConfig
from reyn.core.kernel.phase_router_host import PhaseRouterLoopHost
from reyn.llm.llm import LLMToolCallResult
from reyn.llm.pricing import TokenUsage
from reyn.runtime.router_loop import RouterLoop
from tests._support.router_loop import FakeEventLog, FakeRouterHost, text_result
from tests._support.router_loop import ScriptedLLM as _ScriptedLLM

_USAGE = TokenUsage(prompt_tokens=10, completion_tokens=5)


def _tool_exhauster(n: int) -> LLMToolCallResult:
    return LLMToolCallResult(
        content=None,
        tool_calls=[{
            "id": f"tc_{n}", "type": "function",
            "function": {"name": "_exhauster", "arguments": json.dumps({"n": n})},
        }],
        finish_reason="tool_calls",
        usage=_USAGE,
    )


# ── Phase axis ────────────────────────────────────────────────────────────────


def _phase_host() -> PhaseRouterLoopHost:
    return PhaseRouterLoopHost(
        control_ir_executor=None,
        events=FakeEventLog(),
        phase="act",
        decl=None,
        allowed_ops=None,
        default_sandbox_policy=None,
        agent_name="agent",
        agent_role="assistant",
        output_language="en",
        resolve_model_fn=lambda n: n,
        turn_budget_engine=None,  # no cumulative force-close; limit-deny only
    )


@pytest.mark.asyncio
async def test_phase_limit_deny_sets_forced_close_result() -> None:
    """Tier 2: #1496 phase axis — limit-deny force-close sets forced_close_result
    on PhaseRouterLoopHost for checkpoint persistence."""
    host = _phase_host()
    on_limit = OnLimitConfig(mode="unattended")

    llm = _ScriptedLLM([_tool_exhauster(0), text_result("phase done; artifact at /ws/out")])
    loop = RouterLoop(
        host=host, chain_id="chain-phase", max_iterations=1,
        llm_caller=llm, on_limit=on_limit,
    )
    await loop.run_loop(
        messages=[{"role": "user", "content": "execute phase"}],
        tools=[], _univ_enabled=False,
    )

    fc = host.forced_close_result
    assert fc is not None
    assert getattr(fc, "content", None) == "phase done; artifact at /ws/out"

    # limit_denied event on host.events (phase host owns events directly)
    limit_ev = [e for e in host.events.emitted if e.get("type") == "limit_denied"]
    (ev,) = limit_ev
    assert ev["kind"] == "max_iterations"
    # PhaseRouterLoopHost.put_outbox is a no-op; don't check outbox


@pytest.mark.asyncio
async def test_phase_limit_deny_no_fc_result_when_wrap_up_empty() -> None:
    """Tier 2: #1496 phase axis — forced_close_result NOT set when wrap-up is
    empty (prevents empty-consolidation checkpoint and spurious phase re-entry)."""
    host = _phase_host()
    on_limit = OnLimitConfig(mode="unattended")

    llm = _ScriptedLLM([_tool_exhauster(0)])
    loop = RouterLoop(
        host=host, chain_id="chain-phase-empty", max_iterations=1,
        llm_caller=llm, on_limit=on_limit,
    )
    await loop.run_loop(
        messages=[{"role": "user", "content": "execute phase"}],
        tools=[], _univ_enabled=False,
    )

    assert host.forced_close_result is None
