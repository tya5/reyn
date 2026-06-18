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
from reyn.runtime.planner import Plan, PlanStep, _PlanStepHost
from reyn.runtime.router_loop import RouterLoop
from tests.test_router_loop import FakeEventLog, FakeRouterHost, _ScriptedLLM, text_result

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


# ── Plan axis ─────────────────────────────────────────────────────────────────


def _plan_host(parent: FakeRouterHost) -> _PlanStepHost:
    step = PlanStep(id="s1", description="test step", tools=())
    plan = Plan(goal="g", steps=(step,))
    return _PlanStepHost(
        plan=plan, step=step, prior_results={}, parent=parent,
        turn_budget_engine=None,  # no cumulative force-close; limit-deny only
    )


@pytest.mark.asyncio
async def test_plan_step_limit_deny_sets_forced_close_result() -> None:
    """Tier 2: #1496 plan axis — limit-deny force-close sets forced_close_result
    on _PlanStepHost so the planner can use the consolidation as step output."""
    parent = FakeRouterHost()
    host = _plan_host(parent)
    on_limit = OnLimitConfig(mode="unattended")

    # exhaust + wrap-up text on the force-close call
    llm = _ScriptedLLM([_tool_exhauster(0), text_result("plan step done; files at /out")])
    loop = RouterLoop(
        host=host, chain_id="chain-plan", max_iterations=1,
        llm_caller=llm, on_limit=on_limit,
    )
    await loop.run_loop(
        messages=[{"role": "user", "content": "execute step"}],
        tools=[], _univ_enabled=False,
    )

    # forced_close_result set with wrap-up content
    fc = host.forced_close_result
    assert fc is not None, "forced_close_result must be set after limit-deny wrap-up"
    assert getattr(fc, "content", None) == "plan step done; files at /out"

    # _PlanStepHost captures agent text in captured_text (not outbox list)
    assert host.captured_text == "plan step done; files at /out"

    # limit_denied event on parent.events (plan step delegates to parent)
    limit_ev = [e for e in parent.events.emitted if e.get("type") == "limit_denied"]
    (ev,) = limit_ev
    assert ev["kind"] == "max_iterations"


@pytest.mark.asyncio
async def test_plan_step_limit_deny_no_fc_result_when_wrap_up_empty() -> None:
    """Tier 2: #1496 plan axis — forced_close_result NOT set when wrap-up
    returns no text (empty-checkpoint guard prevents spurious re-entry)."""
    parent = FakeRouterHost()
    host = _plan_host(parent)
    on_limit = OnLimitConfig(mode="unattended")

    # exhaust only; scripted LLM returns tool call on wrap-up → content=None
    llm = _ScriptedLLM([_tool_exhauster(0)])
    loop = RouterLoop(
        host=host, chain_id="chain-plan-empty", max_iterations=1,
        llm_caller=llm, on_limit=on_limit,
    )
    await loop.run_loop(
        messages=[{"role": "user", "content": "execute step"}],
        tools=[], _univ_enabled=False,
    )

    # forced_close_result NOT set — would trigger empty checkpoint re-entry
    assert host.forced_close_result is None
    assert host.captured_text == ""  # no agent text captured


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
