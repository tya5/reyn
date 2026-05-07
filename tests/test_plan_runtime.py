"""Tier 2: PlanRuntime thin-wrapper parity (ADR-0023 Phase 2 step 5).

Step 5 ships PlanRuntime as a peer-to-OSRuntime API surface that
delegates to ``execute_plan`` verbatim. Phase 1 lifecycle behavior must
survive unchanged so Step 6 can migrate ``dispatch_plan_tool`` to use
this class without behavior change.

Reuses the _StubRouterLoop fixture from test_plan_lifecycle_crash via
direct import (= same shape, no real LLM).
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from reyn.chat.planner import Plan, PlanStep
from reyn.plan import PlanResumePlan, PlanRuntime


# ── stub host (= mirror test_plan_lifecycle_crash._RecordingHost) ───────


class _RecordingEvents:
    def __init__(self) -> None:
        self.emitted: list[tuple[str, dict]] = []

    def emit(self, kind: str, **fields: Any) -> None:
        self.emitted.append((kind, fields))


class _RecordingHost:
    def __init__(self) -> None:
        self.events = _RecordingEvents()
        self.plan_started_calls: list[dict] = []
        self.plan_completed_calls: list[dict] = []
        self.plan_aborted_calls: list[dict] = []

    async def record_plan_started(self, *, plan_id: str, goal: str, n_steps: int) -> None:
        self.plan_started_calls.append(
            {"plan_id": plan_id, "goal": goal, "n_steps": n_steps}
        )

    async def record_plan_completed(self, *, plan_id: str) -> None:
        self.plan_completed_calls.append({"plan_id": plan_id})

    async def record_plan_aborted(self, *, plan_id: str, reason: str = "") -> None:
        self.plan_aborted_calls.append({"plan_id": plan_id, "reason": reason})


class _StubRouterLoop:
    _behavior: str = "noop"

    def __init__(self, *, host: Any, **kwargs: Any) -> None:
        self.host = host

    @property
    def total_usage(self) -> Any:
        from reyn.llm.pricing import TokenUsage
        return TokenUsage()

    async def run(self, *, user_text: str, history: list) -> None:
        await self.host.put_outbox(kind="agent", text="step output", meta={})
        return None


@pytest.fixture(autouse=True)
def _stub_router_loop(monkeypatch: Any):
    import reyn.chat.planner as planner_mod

    monkeypatch.setattr(planner_mod, "RouterLoop", _StubRouterLoop)
    _StubRouterLoop._behavior = "noop"
    yield


def _simple_plan() -> Plan:
    return Plan(
        goal="test goal",
        steps=(
            PlanStep(id="s1", description="first", tools=()),
            PlanStep(id="s2", description="second", tools=(), depends_on=("s1",)),
        ),
    )


# ── PlanRuntime wrapper parity ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_plan_runtime_run_delegates_to_execute_plan() -> None:
    """Tier 2: PlanRuntime.run() emits the same plan_started + plan_completed
    sequence execute_plan does (= behavior parity, Phase 1 invariant)."""
    host = _RecordingHost()
    plan = _simple_plan()

    runtime = PlanRuntime(plan, host=host, chain_id="c0")
    result = await runtime.run()

    assert len(host.plan_started_calls) == 1
    assert len(host.plan_completed_calls) == 1
    assert host.plan_aborted_calls == []
    assert result.text


@pytest.mark.asyncio
async def test_plan_runtime_accepts_optional_constructor_args() -> None:
    """Tier 2: PlanRuntime accepts plan_id / budget / router_model /
    resume_plan in its constructor surface (= ADR-0023 §3.4 contract).
    Step 5 doesn't act on them yet — Steps 6/7 do."""
    host = _RecordingHost()
    plan = _simple_plan()
    runtime = PlanRuntime(
        plan, host=host, chain_id="c0",
        plan_id="custom_id_123",
        budget=None,
        router_model="strong",
        resume_plan=None,
    )
    assert runtime.plan_id == "custom_id_123"
    assert runtime.chain_id == "c0"
    assert runtime.resume_plan is None


@pytest.mark.asyncio
async def test_plan_runtime_run_returns_plan_execution_result() -> None:
    """Tier 2: PlanRuntime.run returns the same PlanExecutionResult shape
    execute_plan does."""
    host = _RecordingHost()
    plan = _simple_plan()
    runtime = PlanRuntime(plan, host=host, chain_id="c0")
    result = await runtime.run()

    # Shape parity with PlanExecutionResult
    assert hasattr(result, "text")
    assert hasattr(result, "step_results")
    assert hasattr(result, "step_failures")
    assert hasattr(result, "usage")


# ── PlanResumePlan dataclass shape ────────────────────────────────────────


def test_plan_resume_plan_dataclass_shape() -> None:
    """Tier 2: PlanResumePlan exposes the documented fields (= analyzer
    output contract for Step 7). Frozen so accidental mutation surfaces."""
    from reyn.plan import PlanStepState

    rp = PlanResumePlan(
        plan_id="p001",
        chain_id="c0",
        goal="g",
        n_steps=2,
        decomposition_artifact_path=None,
        step_states=(
            PlanStepState(step_id="s1", state="completed_with_result", result_text="r1"),
            PlanStepState(step_id="s2", state="pending"),
        ),
    )
    assert rp.plan_id == "p001"
    assert "s1" in rp.committed_step_ids
    assert rp.pending_step_ids == ("s2",)
    assert rp.step_result_map() == {"s1": "r1"}

    with pytest.raises((AttributeError, Exception)):
        rp.plan_id = "mutate"  # frozen
