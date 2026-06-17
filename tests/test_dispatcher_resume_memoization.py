"""Tier 2: OS invariant — dispatch_tool memoizes against ResumePlan.committed_steps.

When forward-replay resume is in progress, the runtime threads a
ResumePlan into DispatchContext. dispatch_tool must consult
``committed_steps`` before invoking — a matching
(op_invocation_id + args_hash) means the op has already committed to
the WAL during the prior run, and re-executing would either:
  - duplicate a side effect (file write, mcp call, etc.), or
  - waste an LLM call.

The memo result must:
  - Reproduce the original return value (success path) or error shape
    (failure path).
  - Skip the invoker entirely (verifiable by spy).
  - Skip step-event emission (the original step_completed already
    exists in the WAL — re-emitting would duplicate seq).

Drift detection: if args_hash differs (LLM emitted a structurally
different op shape this resume), the memo is bypassed and the op runs
fresh. This is the defensive guard against silent reuse of a stale
result.

These tests are written FIRST and EXPECTED TO FAIL until the
implementation lands in dispatch_tool. Failing → green confirms the
contract is implemented correctly without overscoping.
"""
from __future__ import annotations

import asyncio
from typing import Any

from reyn.dispatch import DispatchContext, dispatch_tool
from reyn.skill.skill_resume_analyzer import (
    CommittedStep,
    ResumePlan,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeEvents:
    """Captures audit events from dispatch_tool. Plain Fake, no mock."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def emit(self, event_type: str, **data: Any) -> None:
        self.events.append((event_type, data))


_CATALOG = {
    "file":  {"function": {"name": "file"}},
    "shell": {"function": {"name": "shell"}},
    "lint":  {"function": {"name": "lint"}},
}


def _make_ctx(
    *,
    resume_plan: ResumePlan | None = None,
    skill_run_id: str | None = "run_test",
    phase: str | None = "draft",
) -> tuple[DispatchContext, _FakeEvents]:
    ev = _FakeEvents()
    return (
        DispatchContext(
            caller_kind="skill_phase",
            caller_id="skill_x.draft",
            chain_id="c1",
            tool_catalog=_CATALOG,
            events=ev,
            skill_run_id=skill_run_id,
            phase=phase,
            resume_plan=resume_plan,
        ),
        ev,
    )


def _plan_with(steps: list[CommittedStep]) -> ResumePlan:
    return ResumePlan(
        run_id="run_test",
        skill_name="demo",
        skill_input={},
        current_phase="draft",
        last_phase_artifact_path=None,
        awaiting_intervention_id=None,
        committed_steps=steps,
    )


def _committed_success(
    *, oid: str = "draft.0", op_kind: str = "file",
    args_hash: str = "abc", result: object = None,
    phase: str = "draft", seq: int = 10,
) -> CommittedStep:
    return CommittedStep(
        op_invocation_id=oid, op_kind=op_kind, phase=phase,
        args_hash=args_hash, seq=seq,
        result=result if result is not None else {"ok": True},
    )


def _committed_failure(
    *, oid: str = "draft.0", op_kind: str = "file",
    args_hash: str = "abc", error_kind: str = "permission_denied",
    error_message: str = "nope",
    phase: str = "draft", seq: int = 10,
) -> CommittedStep:
    return CommittedStep(
        op_invocation_id=oid, op_kind=op_kind, phase=phase,
        args_hash=args_hash, seq=seq,
        error_kind=error_kind, error_message=error_message,
    )


# ---------------------------------------------------------------------------
# Memoization happy paths
# ---------------------------------------------------------------------------


def test_memoize_returns_recorded_result_without_invoking():
    """Tier 2: matching CommittedStep → return memoized result, invoker is NOT called."""
    invoker_called = []

    async def invoker(args):
        invoker_called.append(True)
        return {"this_should_not": "be_returned"}

    async def go():
        # Pre-compute the args_hash dispatch_tool would generate so the
        # memo matches.
        from reyn.dispatch.dispatcher import _compute_args_hash
        args = {"op": "write", "path": "x.txt", "content": "y"}
        args_hash = _compute_args_hash(args)
        plan = _plan_with([
            _committed_success(
                oid="draft.0", op_kind="file",
                args_hash=args_hash, result={"saved": "x.txt"},
            ),
        ])
        ctx, _ = _make_ctx(resume_plan=plan)
        return await dispatch_tool(
            name="file", args=args, ctx=ctx, invoker=invoker,
            op_invocation_id="draft.0",
        )

    result = asyncio.run(go())
    assert result == {"status": "ok", "data": {"saved": "x.txt"}}
    assert invoker_called == [], "invoker must NOT be called when memoized"


def test_memoize_failure_step_returns_error_shape():
    """Tier 2: matching CommittedStep with error_kind → error result reproducing the original failure shape."""
    async def invoker(args):
        raise AssertionError("invoker must not be called")

    async def go():
        from reyn.dispatch.dispatcher import _compute_args_hash
        args = {"op": "write", "path": "x", "content": "y"}
        args_hash = _compute_args_hash(args)
        plan = _plan_with([
            _committed_failure(
                oid="draft.0", op_kind="file", args_hash=args_hash,
                error_kind="permission_denied", error_message="nope",
            ),
        ])
        ctx, _ = _make_ctx(resume_plan=plan)
        return await dispatch_tool(
            name="file", args=args, ctx=ctx, invoker=invoker,
            op_invocation_id="draft.0",
        )

    result = asyncio.run(go())
    assert result["status"] == "error"
    assert result["error"]["kind"] == "permission_denied"
    assert "nope" in result["error"]["message"]


# ---------------------------------------------------------------------------
# No-match cases — fall through to fresh execution
# ---------------------------------------------------------------------------


def test_no_match_in_committed_steps_falls_through_to_invoker():
    """Tier 2: ResumePlan with no matching op_invocation_id → fresh invocation."""
    async def invoker(args):
        return {"fresh": True}

    async def go():
        plan = _plan_with([
            # different op_invocation_id
            _committed_success(oid="draft.99"),
        ])
        ctx, _ = _make_ctx(resume_plan=plan)
        return await dispatch_tool(
            name="file", args={"op": "write", "path": "x", "content": "y"},
            ctx=ctx, invoker=invoker, op_invocation_id="draft.0",
        )

    result = asyncio.run(go())
    assert result == {"status": "ok", "data": {"fresh": True}}


def test_args_hash_mismatch_falls_through_drift_detection():
    """Tier 2: matching op_invocation_id but DIFFERENT args_hash → bypass memo, invoke fresh.

    This is the drift-detection guard: if the LLM emits a different
    args shape this resume than it did originally, the recorded result
    is no longer applicable. Re-execute.
    """
    invoker_called = []

    async def invoker(args):
        invoker_called.append(True)
        return {"executed_fresh": True}

    async def go():
        plan = _plan_with([
            _committed_success(
                oid="draft.0", op_kind="file",
                args_hash="stale_hash_that_does_not_match",
                result={"old": "value"},
            ),
        ])
        ctx, _ = _make_ctx(resume_plan=plan)
        return await dispatch_tool(
            name="file", args={"op": "write", "path": "x", "content": "y"},
            ctx=ctx, invoker=invoker, op_invocation_id="draft.0",
        )

    result = asyncio.run(go())
    assert invoker_called == [True], "drift must trigger fresh execution"
    assert result["data"] == {"executed_fresh": True}


def test_resume_plan_none_normal_execution():
    """Tier 2: backward compat — resume_plan=None → no memo lookup, normal execution."""
    invoker_called = []

    async def invoker(args):
        invoker_called.append(True)
        return {"normal": True}

    async def go():
        ctx, _ = _make_ctx(resume_plan=None)
        return await dispatch_tool(
            name="file", args={"op": "write", "path": "x", "content": "y"},
            ctx=ctx, invoker=invoker, op_invocation_id="draft.0",
        )

    result = asyncio.run(go())
    assert invoker_called == [True]
    assert result["data"] == {"normal": True}


# ---------------------------------------------------------------------------
# Event emission semantics
# ---------------------------------------------------------------------------


def test_memoized_call_emits_step_memoized_audit_event():
    """Tier 2: memoization emits a `step_memoized` audit event for forensics, distinct from `tool_called` / `tool_returned`.

    `step_memoized` lets operators see "this op did not re-execute on
    resume" in the audit log without inferring it from the absence of
    other events.
    """
    async def invoker(args):
        raise AssertionError("invoker must not be called")

    async def go():
        from reyn.dispatch.dispatcher import _compute_args_hash
        args = {"op": "write", "path": "x", "content": "y"}
        args_hash = _compute_args_hash(args)
        plan = _plan_with([
            _committed_success(args_hash=args_hash, result={"v": 1}),
        ])
        ctx, ev = _make_ctx(resume_plan=plan)
        await dispatch_tool(
            name="file", args=args, ctx=ctx, invoker=invoker,
            op_invocation_id="draft.0",
        )
        return ev

    ev = asyncio.run(go())
    types = [t for t, _ in ev.events]
    assert "step_memoized" in types, (
        f"step_memoized must be emitted on memoized call; got {types}"
    )
    # The pre/post audit events are NOT emitted (the recorded WAL
    # entries already exist; re-emitting would duplicate).
    assert "tool_called" not in types
    assert "tool_returned" not in types


def test_memoized_call_does_not_emit_wal_step_events(tmp_path):
    """Tier 2: memoization skips ``state_log.append`` — re-emitting step_completed would duplicate the original WAL entry that the memo is reproducing.

    Verified by wiring a real StateLog into the context and confirming
    no step events appear after the memoized call.
    """
    from reyn.core.events.state_log import StateLog
    log = StateLog(tmp_path / "wal.jsonl")

    async def invoker(args):
        raise AssertionError("invoker must not be called")

    async def go():
        from reyn.dispatch.dispatcher import _compute_args_hash
        args = {"op": "write", "path": "x", "content": "y"}
        args_hash = _compute_args_hash(args)
        plan = _plan_with([
            _committed_success(args_hash=args_hash, result={"v": 1}),
        ])
        ctx, _ = _make_ctx(resume_plan=plan)
        # Inject the state_log for this test
        ctx.state_log = log
        await dispatch_tool(
            name="file", args=args, ctx=ctx, invoker=invoker,
            op_invocation_id="draft.0",
        )

    asyncio.run(go())
    # No step events were appended by the memoized call
    kinds = [e["kind"] for e in log.iter_from(0)]
    assert kinds == [], f"memoized call must not append step events; got {kinds}"


def test_non_memoized_call_with_resume_plan_still_emits_normally():
    """Tier 2: resume_plan set but committed_steps empty → fresh execution emits normal audit events.

    Defensive: a stale or partial resume plan must not silently disable
    step-event emission for the new ops that come after the cutover.
    """
    async def invoker(args):
        return {"new": True}

    async def go():
        plan = _plan_with([])  # no committed steps
        ctx, ev = _make_ctx(resume_plan=plan)
        await dispatch_tool(
            name="file", args={"op": "write", "path": "x", "content": "y"},
            ctx=ctx, invoker=invoker, op_invocation_id="draft.5",
        )
        return ev

    ev = asyncio.run(go())
    types = [t for t, _ in ev.events]
    # Normal pre/post events for a side_effect op
    assert "tool_called" in types
    assert "tool_returned" in types
    assert "step_memoized" not in types


# ---------------------------------------------------------------------------
# Phase scoping
# ---------------------------------------------------------------------------


def test_committed_step_in_different_phase_does_not_match():
    """Tier 2: even with matching op_invocation_id + args_hash, a CommittedStep from a different phase does not memoize the current call.

    op_invocation_ids reset per phase (`<phase>.<idx>`), so they may
    collide across phases. The phase field must be part of the match.
    """
    invoker_called = []

    async def invoker(args):
        invoker_called.append(True)
        return {"fresh": True}

    async def go():
        from reyn.dispatch.dispatcher import _compute_args_hash
        args = {"op": "write", "path": "x", "content": "y"}
        args_hash = _compute_args_hash(args)
        plan = _plan_with([
            _committed_success(
                oid="draft.0", phase="other_phase",
                args_hash=args_hash, result={"old": "value"},
            ),
        ])
        ctx, _ = _make_ctx(resume_plan=plan, phase="draft")
        return await dispatch_tool(
            name="file", args=args, ctx=ctx, invoker=invoker,
            op_invocation_id="draft.0",
        )

    result = asyncio.run(go())
    assert invoker_called == [True], (
        "phase mismatch must not memoize across phases"
    )
    assert result["data"] == {"fresh": True}
