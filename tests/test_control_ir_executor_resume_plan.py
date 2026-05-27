"""Tier 2: OS invariant — ControlIRExecutor threads resume_plan into DispatchContext.

D3b-1 wired memoization in dispatch_tool when DispatchContext has a
``resume_plan`` set. D3b-2 wires the layer above: ControlIRExecutor
must accept a ``resume_plan`` constructor param and propagate it to
the per-execute DispatchContext, so memoization is reachable from the
real skill execution path.

The contract is small (single-purpose plumbing layer) but easy to
break silently — a regression here would silently disable resume.

Tests pin:
  - Constructor accepts resume_plan
  - DispatchContext built by execute() carries resume_plan through
  - resume_plan=None means normal execution (backward compat)
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from reyn.events.events import EventLog
from reyn.kernel.control_ir_executor import ControlIRExecutor
from reyn.permissions.permissions import PermissionDecl
from reyn.schemas.models import FileIROp
from reyn.skill.skill_resume_analyzer import (
    CommittedStep,
    ResumePlan,
)
from reyn.workspace.workspace import Workspace


def _executor(
    tmp_path: Path,
    *,
    resume_plan: ResumePlan | None = None,
    skill_run_id: str | None = "run_001",
) -> tuple[ControlIRExecutor, EventLog]:
    from reyn.permissions.permissions import PermissionResolver
    events = EventLog()
    ws = Workspace(events=events)
    resolver = PermissionResolver(
        config_permissions={},
        project_root=tmp_path,
        interactive=False,
    )
    return ControlIRExecutor(
        ws, events,
        permission_resolver=resolver,
        skill_name="test_skill",
        chain_id="c1",
        skill_run_id=skill_run_id,
        resume_plan=resume_plan,
    ), events


def _plan_with(steps: list[CommittedStep]) -> ResumePlan:
    return ResumePlan(
        run_id="run_001",
        skill_name="test_skill",
        skill_input={},
        current_phase="draft",
        last_phase_artifact_path=None,
        awaiting_intervention_id=None,
        committed_steps=steps,
    )


# ---------------------------------------------------------------------------
# Constructor + propagation
# ---------------------------------------------------------------------------


def test_constructor_accepts_resume_plan(tmp_path):
    """Tier 2: ControlIRExecutor constructor accepts resume_plan param without error."""
    plan = _plan_with([])
    executor, _ = _executor(tmp_path, resume_plan=plan)
    # Stored for propagation; introspectable for downstream coordination
    assert executor.resume_plan is plan


def test_constructor_default_none(tmp_path):
    """Tier 2: backward compat — default resume_plan is None."""
    executor, _ = _executor(tmp_path)
    assert executor.resume_plan is None


def test_resume_plan_is_propagated_to_dispatch_context(tmp_path, monkeypatch):
    """Tier 2: when execute() runs, the DispatchContext it builds carries the resume_plan.

    Verified by exercising memoization end-to-end: a CommittedStep that
    matches the op being executed must be memoized (invoker skipped,
    recorded result substituted into the result envelope). If
    resume_plan didn't propagate, the op would execute fresh and no
    memo marker would appear.
    """
    from reyn.dispatch.dispatcher import _compute_args_hash

    monkeypatch.chdir(tmp_path)

    # The args_hash must match what dispatch_tool computes for the op.
    # ControlIRExecutor passes op.model_dump(exclude={"kind"}) as args.
    op = FileIROp(
        kind="file", op="write", path="x.txt", content="fresh",
    )
    op_args = op.model_dump(exclude={"kind"})
    args_hash = _compute_args_hash(op_args)

    plan = _plan_with([
        CommittedStep(
            op_invocation_id="draft.0",
            op_kind="file",
            phase="draft",
            args_hash=args_hash,
            seq=10,
            result={"path": "x.txt", "memo_marker": True},
        ),
    ])
    executor, ev = _executor(tmp_path, resume_plan=plan)

    async def go():
        return await executor.execute(
            ops=[op], phase="draft", decl=PermissionDecl(),
            allowed_ops={"file"},
        )

    results = asyncio.run(go())
    assert results, "expected at least one result"
    # Memoization round-trip: dispatch_tool returned the recorded result;
    # ControlIRExecutor unwraps {"status": "ok", "data": ...} into the
    # op_result that's appended to the results list.
    assert results[0].get("memo_marker") is True
    # And step_memoized was emitted to the audit log
    types = [e.type for e in ev.all()]
    assert "step_memoized" in types


def test_resume_plan_none_fresh_execution(tmp_path, monkeypatch):
    """Tier 2: with resume_plan=None, ops execute fresh — no memoized result returned.

    Distinguishes the default path from the memoized path. We don't
    require the actual filesystem write (Workspace setup varies); we
    verify only that the dispatch result does NOT contain the memo
    marker we'd attach if memoization were active.
    """
    monkeypatch.chdir(tmp_path)
    executor, _ = _executor(tmp_path, resume_plan=None)
    op = FileIROp(
        kind="file", op="write", path="written.txt", content="fresh",
    )

    async def go():
        return await executor.execute(
            ops=[op], phase="draft", decl=PermissionDecl(),
            allowed_ops={"file"},
        )

    results = asyncio.run(go())
    assert results, "expected at least one result from fresh execution"
    # Fresh execution — no recorded result was substituted in.
    assert "memo_marker" not in (results[0] or {})


def test_resume_plan_with_no_matching_step_falls_through(tmp_path, monkeypatch):
    """Tier 2: resume_plan set but no CommittedStep matches → fresh execution.

    Pinned: the *presence* of a resume_plan does not silently disable
    execution; only a true match memoizes.
    """
    monkeypatch.chdir(tmp_path)
    plan = _plan_with([
        CommittedStep(
            op_invocation_id="draft.99",  # different invocation id
            op_kind="file",
            phase="draft",
            args_hash="some_other_hash",
            seq=10,
            result={"would_not": "be_returned"},
        ),
    ])
    executor, _ = _executor(tmp_path, resume_plan=plan)
    op = FileIROp(
        kind="file", op="write", path="written.txt", content="fresh",
    )

    async def go():
        return await executor.execute(
            ops=[op], phase="draft", decl=PermissionDecl(),
            allowed_ops={"file"},
        )

    results = asyncio.run(go())
    # No memoization — op executed fresh, recorded marker NOT in result
    assert results, "expected at least one result"
    assert "would_not" not in (results[0] or {})
