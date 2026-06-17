"""Tier 2: OS invariant — OSRuntime.run() finally clause exception-aware completion.

Background: OSRuntime's ``finally`` clause must distinguish between
"the run finished" (= snapshot can be removed) and "the run was
interrupted" (= snapshot must persist for resume). The exception
type that propagates out of the try block determines which.

Categorization:

  ┌─────────────────────────────────────────┬─────────────────────┐
  │ Exit pattern                            │ Lifecycle action    │
  ├─────────────────────────────────────────┼─────────────────────┤
  │ Normal return                           │ complete() ✓        │
  │ WorkflowAbortedError (deliberate abort) │ complete() ✓        │
  │ BudgetExceeded → returns RunResult      │ complete() ✓        │
  │ CancelledError (Ctrl-C / Task.cancel()) │ skip (preserve)     │
  │ KeyboardInterrupt                       │ skip (preserve)     │
  │ RuntimeError / generic Exception        │ skip (preserve)     │
  └─────────────────────────────────────────┴─────────────────────┘

Why preserve on transient failures: the snapshot survives so the next
startup's auto-resume can retry the in-flight phase. The user can
discard explicitly via ``/skill discard <id>`` if they want to give up.

Verified end-to-end via WAL events (skill_completed presence/absence)
and the per-skill snapshot file's existence on disk. ``skill_run_interrupted``
audit event is emitted for the preserve cases for forensics.

Reference: PR-runtime-crash-lifecycle (R-D1) in the active plan.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from reyn.budget.budget import BudgetExceeded
from reyn.core.events.state_log import StateLog
from reyn.core.kernel.normalizer import NormalizationResult
from reyn.core.kernel.runtime import (
    OSRuntime,
    RunResult,
    WorkflowAbortedError,
)
from reyn.schemas.models import (
    ControlDecision,
    ControlReason,
    LLMOutput,
    Phase,
    Skill,
    SkillGraph,
)
from reyn.skill.skill_registry import SkillRegistry

_RUN_ID = "run_crash_lifecycle"
_SKILL_NAME = "crash_demo"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_skill() -> Skill:
    """Single-phase skill: a single ``main`` phase with finish allowed."""
    main = Phase(
        name="main", instructions="m",
        input_schema={"type": "object", "properties": {}}, allowed_ops=[],
    )
    return Skill(
        name=_SKILL_NAME, entry_phase="main",
        phases={"main": main},
        graph=SkillGraph(transitions={}, can_finish_phases=["main"]),
        final_output_schema={"type": "object", "properties": {}},
        final_output_name="result",
    )


def _finish_decision() -> NormalizationResult:
    return NormalizationResult(control=ControlDecision(
        type="finish", decision="finish", next_phase=None,
        confidence=1.0, reason=ControlReason(summary="done"),
    ))


def _finish_output() -> LLMOutput:
    return LLMOutput(
        control=ControlDecision(
            type="finish", decision="finish", next_phase=None,
            confidence=1.0, reason=ControlReason(summary="done"),
        ),
        artifact={"type": "result", "data": {"final": True}},
        ops=[],
    )


class _StubRuntime(OSRuntime):
    """OSRuntime stub: ``_execute_phase`` finishes normally OR raises a
    configurable exception on the first call."""

    def __init__(
        self, skill: Skill, *,
        skill_registry: SkillRegistry,
        state_log: StateLog,
        raise_on_first_phase: BaseException | None = None,
    ) -> None:
        super().__init__(
            skill, model="stub/model", run_id=_RUN_ID,
            skill_registry=skill_registry,
            state_log=state_log,
        )
        self._raise = raise_on_first_phase

    async def _execute_phase(
        self,
        current_phase: str,
        artifact: dict,
        candidates: list,
        output_language: str,
        max_phase_retries: int,
        artifact_path: str | None = None,
        rollback_context: dict | None = None,
    ) -> tuple[NormalizationResult, LLMOutput, int]:
        if self._raise is not None:
            raise self._raise
        return _finish_decision(), _finish_output(), 0


def _setup(tmp_path: Path) -> tuple[SkillRegistry, StateLog, Path]:
    state_dir = tmp_path / ".reyn" / "agents" / "alpha" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    registry = SkillRegistry(
        agent_name="alpha", agent_state_dir=state_dir, state_log=log,
    )
    snap_path = state_dir / "skills" / f"{_RUN_ID}.snapshot.json"
    return registry, log, snap_path


# ---------------------------------------------------------------------------
# complete() called: normal exit / deliberate abort / budget exceeded
# ---------------------------------------------------------------------------


def test_normal_completion_calls_complete(tmp_path: Path):
    """Tier 2: normal return → ``skill_completed`` in WAL, snapshot removed."""
    registry, log, snap_path = _setup(tmp_path)
    rt = _StubRuntime(
        _make_skill(), skill_registry=registry, state_log=log,
    )
    result = asyncio.run(rt.run({"type": "input", "data": {}}))

    assert isinstance(result, RunResult) and result.ok
    kinds = [e["kind"] for e in log.iter_from(0)]
    assert "skill_completed" in kinds
    assert "skill_run_interrupted" not in [
        ev.type for ev in rt.events.all()
    ]
    assert not snap_path.exists()


def test_workflow_aborted_calls_complete(tmp_path: Path):
    """Tier 2: WorkflowAbortedError = deliberate skill abort → still considered done.

    The skill itself decided to abort (e.g. retry_limit exhausted at
    LLM-decision level). Resume would just re-decide-to-abort, so the
    snapshot is removed.
    """
    registry, log, snap_path = _setup(tmp_path)
    rt = _StubRuntime(
        _make_skill(), skill_registry=registry, state_log=log,
        raise_on_first_phase=WorkflowAbortedError("skill aborted"),
    )

    with pytest.raises(WorkflowAbortedError):
        asyncio.run(rt.run({"type": "input", "data": {}}))

    kinds = [e["kind"] for e in log.iter_from(0)]
    assert "skill_completed" in kinds
    assert not snap_path.exists()


def test_budget_exceeded_calls_complete(tmp_path: Path):
    """Tier 2: BudgetExceeded → returns RunResult(status='budget_exceeded') → complete called.

    BudgetExceeded is caught inside ``run()`` and converted to a
    RunResult — the function returns normally (exc_info is None at
    finally). complete() runs as on any normal return.
    """
    registry, log, snap_path = _setup(tmp_path)
    rt = _StubRuntime(
        _make_skill(), skill_registry=registry, state_log=log,
        raise_on_first_phase=BudgetExceeded(
            dimension="agent", detail="over budget",
        ),
    )

    result = asyncio.run(rt.run({"type": "input", "data": {}}))
    assert result.status == "budget_exceeded"
    kinds = [e["kind"] for e in log.iter_from(0)]
    assert "skill_completed" in kinds
    assert not snap_path.exists()


# ---------------------------------------------------------------------------
# complete() skipped: cancellation / interrupt / unrecoverable error
# ---------------------------------------------------------------------------


def test_cancelled_error_preserves_snapshot(tmp_path: Path):
    """Tier 2: asyncio.CancelledError (Ctrl-C / Task.cancel()) → snapshot preserved.

    No skill_completed event in WAL; per-skill snapshot file remains
    on disk so the next startup's auto-resume can pick it up.
    """
    registry, log, snap_path = _setup(tmp_path)
    rt = _StubRuntime(
        _make_skill(), skill_registry=registry, state_log=log,
        raise_on_first_phase=asyncio.CancelledError(),
    )

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(rt.run({"type": "input", "data": {}}))

    kinds = [e["kind"] for e in log.iter_from(0)]
    assert "skill_completed" not in kinds, (
        f"CancelledError must NOT trigger complete; got {kinds}"
    )
    assert snap_path.exists(), (
        "snapshot must be preserved so auto-resume can retry"
    )
    interrupted = [
        ev for ev in rt.events.all() if ev.type == "skill_run_interrupted"
    ]
    assert interrupted
    assert interrupted[0].data["exc_type"] == "CancelledError"
    assert interrupted[0].data["run_id"] == _RUN_ID


def test_keyboard_interrupt_preserves_snapshot(tmp_path: Path):
    """Tier 2: KeyboardInterrupt → snapshot preserved (= same policy as CancelledError)."""
    registry, log, snap_path = _setup(tmp_path)
    rt = _StubRuntime(
        _make_skill(), skill_registry=registry, state_log=log,
        raise_on_first_phase=KeyboardInterrupt(),
    )

    with pytest.raises(KeyboardInterrupt):
        asyncio.run(rt.run({"type": "input", "data": {}}))

    kinds = [e["kind"] for e in log.iter_from(0)]
    assert "skill_completed" not in kinds
    assert snap_path.exists()
    interrupted = [
        ev for ev in rt.events.all() if ev.type == "skill_run_interrupted"
    ]
    assert interrupted
    assert interrupted[0].data["exc_type"] == "KeyboardInterrupt"


def test_runtime_error_preserves_snapshot(tmp_path: Path):
    """Tier 2: generic RuntimeError → snapshot preserved.

    Transient runtime failures (race condition, network blip, bug)
    leave the snapshot intact so the next startup's auto-resume can
    retry. If the failure repeats, the user can ``/skill discard``
    explicitly.
    """
    registry, log, snap_path = _setup(tmp_path)
    rt = _StubRuntime(
        _make_skill(), skill_registry=registry, state_log=log,
        raise_on_first_phase=RuntimeError("transient blip"),
    )

    with pytest.raises(RuntimeError, match="transient blip"):
        asyncio.run(rt.run({"type": "input", "data": {}}))

    kinds = [e["kind"] for e in log.iter_from(0)]
    assert "skill_completed" not in kinds
    assert snap_path.exists()
    interrupted = [
        ev for ev in rt.events.all() if ev.type == "skill_run_interrupted"
    ]
    assert interrupted
    assert interrupted[0].data["exc_type"] == "RuntimeError"
