"""Tier 2: dispatch_plan_tool migrated to PlanRuntime + artifact lifecycle
(ADR-0023 Phase 2 step 6).

Phase 2 lifecycle ordering invariant:
  1. validate plan
  2. allocate plan_id
  3. write decomposition artifact (= P5 SSoT)
  4. construct PlanRuntime(plan_id=plan_id) → run
  5. on clean exit (success / WorkflowAbortedError): delete artifact
  6. on crash / cancel: preserve artifact for restart cleanup

Also pins plan_step_* WAL routing (= record_plan_step_*) replacing the
forensic-only events.emit path.
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from reyn.chat.planner import (
    Plan,
    PlanStep,
    dispatch_plan_tool,
)

# ── stub host with full Step 6 surface ────────────────────────────────────


class _RecordingEvents:
    def __init__(self) -> None:
        self.emitted: list[tuple[str, dict]] = []

    def emit(self, kind: str, **fields: Any) -> None:
        self.emitted.append((kind, fields))


class _RecordingHost:
    """Host that records every Phase 2 step-6-relevant call.

    Captures plan_id seen by record_plan_started + write_plan_decomposition
    so we can assert lifecycle ordering, and tracks step events so we can
    assert WAL routing parity.
    """

    def __init__(self) -> None:
        self.events = _RecordingEvents()
        self.plan_started_calls: list[dict] = []
        self.plan_completed_calls: list[dict] = []
        self.plan_aborted_calls: list[dict] = []
        self.plan_step_started_calls: list[dict] = []
        self.plan_step_completed_calls: list[dict] = []
        self.plan_step_failed_calls: list[dict] = []
        self.write_decomp_calls: list[dict] = []
        self.delete_decomp_calls: list[dict] = []
        self.call_order: list[str] = []  # for ordering assertions

    async def record_plan_started(self, *, plan_id, goal, n_steps):
        self.plan_started_calls.append(
            {"plan_id": plan_id, "goal": goal, "n_steps": n_steps}
        )
        self.call_order.append(f"plan_started:{plan_id}")

    async def record_plan_completed(self, *, plan_id):
        self.plan_completed_calls.append({"plan_id": plan_id})
        self.call_order.append(f"plan_completed:{plan_id}")

    async def record_plan_aborted(self, *, plan_id, reason=""):
        self.plan_aborted_calls.append({"plan_id": plan_id, "reason": reason})

    async def record_plan_step_started(
        self, *, plan_id, step_id, depends_on, n_tools,
    ):
        self.plan_step_started_calls.append({
            "plan_id": plan_id, "step_id": step_id,
            "depends_on": list(depends_on), "n_tools": n_tools,
        })

    async def record_plan_step_completed(self, *, plan_id, step_id, content_len):
        self.plan_step_completed_calls.append({
            "plan_id": plan_id, "step_id": step_id, "content_len": content_len,
        })

    async def record_plan_step_failed(self, *, plan_id, step_id, error):
        self.plan_step_failed_calls.append({
            "plan_id": plan_id, "step_id": step_id, "error": error,
        })

    async def write_plan_decomposition(self, *, plan_id, plan):
        self.write_decomp_calls.append({"plan_id": plan_id, "plan": plan})
        self.call_order.append(f"write_decomp:{plan_id}")
        return f"/fake/path/{plan_id}/decomposition.json"

    async def delete_plan_decomposition(self, *, plan_id):
        self.delete_decomp_calls.append({"plan_id": plan_id})
        self.call_order.append(f"delete_decomp:{plan_id}")


class _StubRouterLoop:
    _behavior = "noop"

    def __init__(self, *, host, **kwargs):
        self.host = host

    @property
    def total_usage(self):
        from reyn.llm.pricing import TokenUsage
        return TokenUsage()

    async def run(self, *, user_text, history):
        if _StubRouterLoop._behavior == "raise:RuntimeError":
            raise RuntimeError("test crash")
        if _StubRouterLoop._behavior == "raise:WorkflowAbortedError":
            from reyn.kernel.runtime import WorkflowAbortedError
            raise WorkflowAbortedError("test abort")
        await self.host.put_outbox(kind="agent", text="ok", meta={})
        return None


@pytest.fixture(autouse=True)
def _stub_router_loop(monkeypatch: Any):
    import reyn.chat.planner as planner_mod
    monkeypatch.setattr(planner_mod, "RouterLoop", _StubRouterLoop)
    _StubRouterLoop._behavior = "noop"
    yield


def _simple_plan_args() -> dict:
    return {
        "goal": "g",
        "steps": [
            {"id": "s1", "description": "first", "tools": []},
            {"id": "s2", "description": "second", "tools": [], "depends_on": ["s1"]},
        ],
    }


# ── lifecycle ordering ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dispatch_writes_decomposition_before_plan_started() -> None:
    """Tier 2: ADR-0023 §3.5 — artifact write MUST precede plan_started so
    any plan in active_plan_ids has a discoverable decomposition."""
    host = _RecordingHost()
    result = await dispatch_plan_tool(
        args=_simple_plan_args(),
        parent_host=host, chain_id="c0",
        available_tool_names=set(),
    )
    assert result["status"] == "ok"
    write_idx = next(
        i for i, c in enumerate(host.call_order) if c.startswith("write_decomp:")
    )
    started_idx = next(
        i for i, c in enumerate(host.call_order) if c.startswith("plan_started:")
    )
    assert write_idx < started_idx


@pytest.mark.asyncio
async def test_dispatch_uses_same_plan_id_across_lifecycle() -> None:
    """Tier 2: the plan_id allocated by dispatch_plan_tool is the same id
    threaded through write_decomp + plan_started + plan_completed +
    delete_decomp (= no auto-allocation in execute_plan when caller
    supplies one)."""
    host = _RecordingHost()
    await dispatch_plan_tool(
        args=_simple_plan_args(),
        parent_host=host, chain_id="c0",
        available_tool_names=set(),
    )
    (only_write,) = host.write_decomp_calls
    (only_started,) = host.plan_started_calls
    (only_completed,) = host.plan_completed_calls
    (only_deleted,) = host.delete_decomp_calls
    plan_id = only_write["plan_id"]
    assert only_started["plan_id"] == plan_id
    assert only_completed["plan_id"] == plan_id
    assert only_deleted["plan_id"] == plan_id


@pytest.mark.asyncio
async def test_dispatch_routes_step_events_through_record_methods() -> None:
    """Tier 2: each step calls record_plan_step_started + completed (=
    WAL persistence). 2-step plan → 2 started + 2 completed calls."""
    host = _RecordingHost()
    await dispatch_plan_tool(
        args=_simple_plan_args(),
        parent_host=host, chain_id="c0",
        available_tool_names=set(),
    )
    (step_s1, step_s2) = host.plan_step_started_calls
    (completed_s1, completed_s2) = host.plan_step_completed_calls
    assert step_s1["step_id"] == "s1"
    assert step_s2["step_id"] == "s2"
    assert step_s2["depends_on"] == ["s1"]
    assert completed_s1["step_id"] == "s1"
    assert completed_s2["step_id"] == "s2"


# ── artifact cleanup branches ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dispatch_deletes_artifact_on_normal_completion() -> None:
    """Tier 2: clean run → artifact cleaned up via delete_plan_decomposition."""
    host = _RecordingHost()
    await dispatch_plan_tool(
        args=_simple_plan_args(),
        parent_host=host, chain_id="c0",
        available_tool_names=set(),
    )
    (only_deleted,) = host.delete_decomp_calls
    assert "plan_id" in only_deleted


@pytest.mark.asyncio
async def test_dispatch_deletes_artifact_when_workflow_abort_caught_per_step() -> None:
    """Tier 2: per-step try/except in execute_plan catches
    WorkflowAbortedError as a step failure — the plan finishes with
    failures recorded, so plan_completed fires and the artifact is
    deleted (= clean exit)."""
    _StubRouterLoop._behavior = "raise:WorkflowAbortedError"
    host = _RecordingHost()
    result = await dispatch_plan_tool(
        args=_simple_plan_args(),
        parent_host=host, chain_id="c0",
        available_tool_names=set(),
    )
    # All steps recorded as failed (= per-step except catches WorkflowAbortedError).
    (failed_s1, failed_s2) = host.plan_step_failed_calls
    assert failed_s1["step_id"] == "s1"
    assert failed_s2["step_id"] == "s2"
    # Plan still completes cleanly; artifact cleaned up.
    (only_completed,) = host.plan_completed_calls
    assert "plan_id" in only_completed
    (only_deleted,) = host.delete_decomp_calls
    assert "plan_id" in only_deleted


@pytest.mark.asyncio
async def test_dispatch_preserves_artifact_on_crash() -> None:
    """Tier 2: generic exception leaves the artifact in place for restart
    cleanup or memo-replay resume."""
    # The internal step's try/except in execute_plan catches RuntimeError
    # as a step failure and continues; to actually crash the runtime we
    # need an exception that escapes that handler. Use cancel-style.
    class _CancellingRouterLoop(_StubRouterLoop):
        async def run(self, *, user_text, history):
            raise asyncio.CancelledError("test cancel")

    import reyn.chat.planner as planner_mod
    original = planner_mod.RouterLoop
    planner_mod.RouterLoop = _CancellingRouterLoop
    try:
        host = _RecordingHost()
        with pytest.raises(asyncio.CancelledError):
            await dispatch_plan_tool(
                args=_simple_plan_args(),
                parent_host=host, chain_id="c0",
                available_tool_names=set(),
            )
        # No delete on cancellation → artifact preserved for resume.
        assert host.delete_decomp_calls == []
    finally:
        planner_mod.RouterLoop = original


# ── error / fallback ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dispatch_records_step_failed_when_step_raises() -> None:
    """Tier 2: when a sub-loop crashes inside execute_plan's per-step
    try/except, the step failure is recorded via record_plan_step_failed
    on the WAL path."""
    _StubRouterLoop._behavior = "raise:RuntimeError"
    host = _RecordingHost()
    result = await dispatch_plan_tool(
        args=_simple_plan_args(),
        parent_host=host, chain_id="c0",
        available_tool_names=set(),
    )
    # All steps should be marked failed (= 2-step plan, both sub-loops crash).
    (failed_s1, failed_s2) = host.plan_step_failed_calls
    assert failed_s1["step_id"] == "s1"
    assert failed_s2["step_id"] == "s2"
    # plan_completed is still emitted because execute_plan only
    # propagates uncaught exceptions; per-step errors are caught.
    (only_completed,) = host.plan_completed_calls
    assert "plan_id" in only_completed


@pytest.mark.asyncio
async def test_dispatch_invalid_plan_returns_error_no_artifact_write() -> None:
    """Tier 2: validation failure short-circuits before any WAL or
    artifact effects (= no plan_id allocated, no write_decomposition)."""
    host = _RecordingHost()
    result = await dispatch_plan_tool(
        args={"goal": "g", "steps": []},  # too few
        parent_host=host, chain_id="c0",
        available_tool_names=set(),
    )
    assert result["status"] == "error"
    assert host.write_decomp_calls == []
    assert host.plan_started_calls == []


# ── backward compat: hosts without Step 6 methods ────────────────────────


class _OldStyleHost:
    """Host that implements only Phase 1's record_plan_started/completed/
    aborted but not the Step 6 additions. Phase 2 must still function
    (defensively)."""

    def __init__(self) -> None:
        self.events = _RecordingEvents()
        self.plan_started_calls: list[dict] = []
        self.plan_completed_calls: list[dict] = []

    async def record_plan_started(self, *, plan_id, goal, n_steps):
        self.plan_started_calls.append({"plan_id": plan_id})

    async def record_plan_completed(self, *, plan_id):
        self.plan_completed_calls.append({"plan_id": plan_id})

    async def record_plan_aborted(self, *, plan_id, reason=""):
        pass


@pytest.mark.asyncio
async def test_dispatch_tolerates_host_without_step6_methods() -> None:
    """Tier 2: host stub that lacks write_plan_decomposition /
    delete_plan_decomposition / record_plan_step_* doesn't break dispatch
    (= AttributeError is swallowed defensively per Phase 1 precedent)."""
    host = _OldStyleHost()
    result = await dispatch_plan_tool(
        args=_simple_plan_args(),
        parent_host=host, chain_id="c0",
        available_tool_names=set(),
    )
    assert result["status"] == "ok"
    (only_started,) = host.plan_started_calls
    assert "plan_id" in only_started
    (only_completed,) = host.plan_completed_calls
    assert "plan_id" in only_completed
