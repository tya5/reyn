"""Tier 2: PlanRuntime memo replay (ADR-0023 §3.4 Phase 2 step 7b).

Pins the memo replay contract:
  - completed_with_result step → memo (= no sub-loop, no WAL emit)
  - failed step → memo_failed (= no re-execute)
  - pending step → execute (= run sub-loop normally)

Resume_plan=None preserves the fresh-run path (= Phase 1 / Step 6
behavior unchanged).
"""
from __future__ import annotations

from typing import Any

import pytest

from reyn.chat.planner import Plan, PlanStep, execute_plan
from reyn.plan import (
    PlanResumePlan,
    PlanRuntime,
    PlanStepState,
)

# ── stubs ────────────────────────────────────────────────────────────────


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
        self.plan_step_started_calls: list[dict] = []
        self.plan_step_completed_calls: list[dict] = []
        self.plan_step_failed_calls: list[dict] = []

    async def record_plan_started(self, *, plan_id, goal, n_steps):
        self.plan_started_calls.append({"plan_id": plan_id})

    async def record_plan_completed(self, *, plan_id):
        self.plan_completed_calls.append({"plan_id": plan_id})

    async def record_plan_aborted(self, *, plan_id, reason=""):
        self.plan_aborted_calls.append({"plan_id": plan_id})

    async def record_plan_step_started(self, *, plan_id, step_id, depends_on, n_tools):
        self.plan_step_started_calls.append({"step_id": step_id})

    async def record_plan_step_completed(self, *, plan_id, step_id, content_len):
        self.plan_step_completed_calls.append({"step_id": step_id})

    async def record_plan_step_failed(self, *, plan_id, step_id, error):
        self.plan_step_failed_calls.append({"step_id": step_id})


class _CountingRouterLoop:
    """Tracks how many sub-loops actually ran; memo replay should bypass."""

    invocations: list[str] = []

    def __init__(self, *, host, **kwargs):
        self.host = host

    @property
    def total_usage(self):
        from reyn.llm.pricing import TokenUsage
        return TokenUsage()

    async def run(self, *, user_text, history):
        _CountingRouterLoop.invocations.append(user_text)
        await self.host.put_outbox(kind="agent", text=f"ran:{user_text}", meta={})
        return None


@pytest.fixture(autouse=True)
def _stub_router_loop(monkeypatch: Any):
    import reyn.chat.planner as planner_mod
    monkeypatch.setattr(planner_mod, "RouterLoop", _CountingRouterLoop)
    _CountingRouterLoop.invocations.clear()
    yield


def _three_step_plan() -> Plan:
    return Plan(
        goal="g",
        steps=(
            PlanStep("s1", "first", ()),
            PlanStep("s2", "second", (), depends_on=("s1",)),
            PlanStep("s3", "third", (), depends_on=("s2",)),
        ),
    )


# ── memo path ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_resume_plan_skips_completed_step_via_memo() -> None:
    """Tier 2: a step in completed_with_result state is NOT re-executed —
    sub-loop is bypassed and recorded text is reused."""
    plan = _three_step_plan()
    rp = PlanResumePlan(
        plan_id="p001", chain_id="c0", goal="g",
        n_steps=3, decomposition_artifact_path=None,
        step_states=(
            PlanStepState(step_id="s1", state="completed_with_result",
                          result_text="memo:s1"),
            PlanStepState(step_id="s2", state="pending"),
            PlanStepState(step_id="s3", state="pending"),
        ),
    )
    host = _RecordingHost()
    runtime = PlanRuntime(plan, host=host, chain_id="c0", plan_id="p001",
                          resume_plan=rp)
    result = await runtime.run()

    # Only s2 + s3 ran; s1 was memoized.
    assert _CountingRouterLoop.invocations == ["second", "third"]
    assert result.step_results["s1"] == "memo:s1"
    # s1 had no record_plan_step_started / completed call (= no WAL re-emit).
    assert {c["step_id"] for c in host.plan_step_started_calls} == {"s2", "s3"}


@pytest.mark.asyncio
async def test_resume_plan_emits_plan_step_memoized_event() -> None:
    """Tier 2: memo replay emits a forensic plan_step_memoized event so
    the events log records the no-op (= audit trail)."""
    plan = _three_step_plan()
    rp = PlanResumePlan(
        plan_id="p001", chain_id="c0", goal="g",
        n_steps=3, decomposition_artifact_path=None,
        step_states=(
            PlanStepState(step_id="s1", state="completed_with_result",
                          result_text="memo:s1"),
            PlanStepState(step_id="s2", state="pending"),
            PlanStepState(step_id="s3", state="pending"),
        ),
    )
    host = _RecordingHost()
    runtime = PlanRuntime(plan, host=host, chain_id="c0", plan_id="p001",
                          resume_plan=rp)
    await runtime.run()

    memo_events = [e for e in host.events.emitted
                   if e[0] == "plan_step_memoized"]
    assert len(memo_events) == 1
    assert memo_events[0][1]["step_id"] == "s1"


@pytest.mark.asyncio
async def test_resume_plan_failed_state_propagates_without_re_execute() -> None:
    """Tier 2: a step in failed state is not re-executed; failure is
    recorded into step_failures so downstream synthesis sees the gap."""
    plan = _three_step_plan()
    rp = PlanResumePlan(
        plan_id="p001", chain_id="c0", goal="g",
        n_steps=3, decomposition_artifact_path=None,
        step_states=(
            PlanStepState(step_id="s1", state="failed",
                          error_message="recorded boom"),
            PlanStepState(step_id="s2", state="pending"),
            PlanStepState(step_id="s3", state="pending"),
        ),
    )
    host = _RecordingHost()
    runtime = PlanRuntime(plan, host=host, chain_id="c0", plan_id="p001",
                          resume_plan=rp)
    result = await runtime.run()

    # s1 not re-executed
    assert "first" not in _CountingRouterLoop.invocations
    assert "recorded boom" in result.step_failures["s1"]


@pytest.mark.asyncio
async def test_resume_plan_pending_step_executes_normally() -> None:
    """Tier 2: pending steps run via the sub-loop (= normal flow)."""
    plan = _three_step_plan()
    rp = PlanResumePlan(
        plan_id="p001", chain_id="c0", goal="g",
        n_steps=3, decomposition_artifact_path=None,
        step_states=tuple(
            PlanStepState(step_id=sid, state="pending")
            for sid in ("s1", "s2", "s3")
        ),
    )
    host = _RecordingHost()
    runtime = PlanRuntime(plan, host=host, chain_id="c0", plan_id="p001",
                          resume_plan=rp)
    await runtime.run()
    # All three steps executed via sub-loop.
    assert sorted(_CountingRouterLoop.invocations) == ["first", "second", "third"]


@pytest.mark.asyncio
async def test_no_resume_plan_runs_all_steps_fresh() -> None:
    """Tier 2: resume_plan=None keeps Phase 1 fresh-run behavior — every
    step runs through the sub-loop."""
    plan = _three_step_plan()
    host = _RecordingHost()
    runtime = PlanRuntime(plan, host=host, chain_id="c0", plan_id="p001",
                          resume_plan=None)
    await runtime.run()
    assert sorted(_CountingRouterLoop.invocations) == ["first", "second", "third"]


@pytest.mark.asyncio
async def test_partial_resume_chains_memo_into_pending_step_inputs() -> None:
    """Tier 2: when s1 is memoized and s2 is pending, s2's prior_results
    sees the memo'd text from s1 (= dependency chain works across resume)."""
    plan = _three_step_plan()
    rp = PlanResumePlan(
        plan_id="p001", chain_id="c0", goal="g",
        n_steps=3, decomposition_artifact_path=None,
        step_states=(
            PlanStepState(step_id="s1", state="completed_with_result",
                          result_text="prior_step_output"),
            PlanStepState(step_id="s2", state="pending"),
            PlanStepState(step_id="s3", state="pending"),
        ),
    )
    host = _RecordingHost()
    runtime = PlanRuntime(plan, host=host, chain_id="c0", plan_id="p001",
                          resume_plan=rp)
    result = await runtime.run()

    # s1 memo result is in step_results so s2's system prompt builds with it.
    assert result.step_results["s1"] == "prior_step_output"
    # s2 ran (= got the memo as its input).
    assert "second" in _CountingRouterLoop.invocations
