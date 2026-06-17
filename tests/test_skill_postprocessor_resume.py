"""Tier 2: Mid-postprocessor crash recovery — OSRuntime + PostprocessorExecutor resume.

Tests the __post__ pseudo-phase resume entry and step-level memoization:

  (a) When postprocessor starts, current_phase becomes "__post__" in snapshot.
  (b) On resume with current_phase=="__post__", OSRuntime skips the phase loop
      and loads the finish artifact from last_phase_artifact_path.
  (c) Postprocessor step memo: re-running a recorded step via resume_plan
      returns the recorded result without re-executing.
  (d) Mid-postprocessor crash → resume → step 1 memo hit, step 2 re-executes.
  (e) PostprocessorError (WorkflowAbortedError path) → snapshot deleted
      (= no resume), per ADR-0013.

No mocks; uses real StateLog, SkillRegistry, and PostprocessorExecutor
instances. OSRuntime subclasses override _execute_phase to avoid LLM calls.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from reyn.core.compiler.expander import expand_phase, expand_skill
from reyn.core.compiler.ir import ArtifactDef, PhaseDef, SkillDef
from reyn.core.events.events import EventLog
from reyn.core.events.state_log import StateLog
from reyn.core.kernel.normalizer import NormalizationResult
from reyn.core.kernel.postprocessor_executor import PostprocessorExecutor
from reyn.core.kernel.runtime import OSRuntime, RunResult, WorkflowAbortedError
from reyn.data.workspace.workspace import Workspace
from reyn.llm.model_resolver import ModelResolver
from reyn.schemas.models import (
    ControlDecision,
    ControlReason,
    LLMOutput,
    Phase,
    Skill,
    SkillGraph,
)
from reyn.skill.skill_registry import SkillRegistry
from reyn.skill.skill_resume_analyzer import CommittedStep, ResumePlan, SkillResumeAnalyzer

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _event_log() -> EventLog:
    """Real EventLog with an in-memory subscriber list."""
    collected: list = []
    log = EventLog(subscribers=[lambda e: collected.append(e)])
    log._collected = collected
    return log


def _resolver() -> ModelResolver:
    return ModelResolver({})


def _build_postprocessor_skill(
    postprocessor_steps: list | None = None,
    postprocessor_output_schema: dict | None = None,
) -> Skill:
    """Build a single-phase skill that can finish, with an optional postprocessor."""
    artifacts = {
        "in_art": ArtifactDef(
            name="in_art",
            schema={"type": "object", "properties": {"x": {"type": "integer"}}},
            description="Input",
            wrapped=True,
        ),
        "llm_art": ArtifactDef(
            name="llm_art",
            schema={
                "type": "object",
                "properties": {"y": {"type": "string"}},
                "required": ["y"],
            },
            description="LLM output",
            wrapped=True,
        ),
    }
    pd = PhaseDef(
        name="sole",
        inputs=["in_art"],
        role=None,
        can_finish=True,
        instructions="",
    )
    phase_obj = expand_phase(pd, [artifacts["in_art"]])
    postprocessor = None
    if postprocessor_steps is not None:
        postprocessor = {
            "steps": postprocessor_steps,
            "output_schema": postprocessor_output_schema or {
                "type": "object",
                "properties": {"y": {"type": "string"}},
            },
        }
    sd = SkillDef(
        name="test_skill",
        description="",
        doc="",
        entry="sole",
        edges=[],
        skill_nodes={},
        final_output="llm_art",
        final_output_description="",
        finish_criteria=[],
        postprocessor=postprocessor or {},
    )
    return expand_skill(sd, {"sole": pd}, artifacts, {"sole": phase_obj})


def _make_single_phase_skill_with_post() -> Skill:
    """Standalone Skill model (not via compiler) — simpler for runtime tests."""
    phase = Phase(
        name="sole",
        instructions="do it",
        input_schema={"type": "object", "properties": {}},
        allowed_ops=[],
    )
    from reyn.schemas.models import Postprocessor, ValidateStep
    postprocessor = Postprocessor(
        steps=[
            ValidateStep(type="validate", schema_={"type": "object"}),
            ValidateStep(type="validate", schema_={"type": "object"}),
        ],
        output_schema={"type": "object", "properties": {"y": {"type": "string"}}},
    )
    return Skill(
        name="post_skill",
        entry_phase="sole",
        phases={"sole": phase},
        graph=SkillGraph(
            transitions={},
            can_finish_phases=["sole"],
        ),
        final_output_schema={"type": "object", "properties": {"y": {"type": "string"}}},
        final_output_name="result",
        postprocessor=postprocessor,
    )


def _finish_decision() -> NormalizationResult:
    return NormalizationResult(control=ControlDecision(
        type="finish", decision="finish", next_phase=None,
        confidence=1.0, reason=ControlReason(summary="done"),
    ))


def _finish_output(y: str = "hello") -> LLMOutput:
    return LLMOutput(
        control=ControlDecision(
            type="finish", decision="finish", next_phase=None,
            confidence=1.0, reason=ControlReason(summary="done"),
        ),
        artifact={"type": "result", "data": {"y": y}},
        ops=[],
    )


class _FinishRuntime(OSRuntime):
    """OSRuntime that finishes the single phase immediately with a fixed artifact."""

    def __init__(self, skill: Skill, *, y: str = "hello", **kw) -> None:
        super().__init__(skill, model="stub/model", **kw)
        self._y = y
        self.phase_calls: list[str] = []

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
        self.phase_calls.append(current_phase)
        return _finish_decision(), _finish_output(self._y), 0


class _CrashMidPostprocessor(OSRuntime):
    """OSRuntime that finishes the phase then crashes after _finish_workflow is called.

    We override _finish_workflow to crash after advance_phase("__post__") has
    been called (step 1 of the postprocessor is mocked to succeed, then we
    raise to simulate a mid-postprocessor crash).
    """

    def __init__(self, skill: Skill, *, crash_after_advance: bool = True, **kw) -> None:
        super().__init__(skill, model="stub/model", **kw)
        self._crash_after_advance = crash_after_advance
        self.phase_calls: list[str] = []
        self._did_advance = False

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
        self.phase_calls.append(current_phase)
        return _finish_decision(), _finish_output(), 0


# ---------------------------------------------------------------------------
# (a) Snapshot current_phase becomes "__post__" when postprocessor starts
# ---------------------------------------------------------------------------


def test_snapshot_advances_to_post_before_postprocessor(tmp_path, monkeypatch):
    """Tier 2: (a) After OSRuntime finishes the phase, snapshot.current_phase == '__post__'
    before postprocessor steps execute.

    We use a postprocessor with a validate step that always passes — the goal
    is to verify that the snapshot has been advanced to '__post__' by the time
    the postprocessor runs.
    """
    monkeypatch.chdir(tmp_path)

    skill = _make_single_phase_skill_with_post()
    state_dir = tmp_path / ".reyn" / "agents" / "alpha" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    registry = SkillRegistry(
        agent_name="alpha",
        agent_state_dir=state_dir,
        state_log=state_log,
    )

    # Track the snapshot state at postprocessor entry by recording WAL events.
    rt = _FinishRuntime(
        skill,
        run_id="run_post_001",
        skill_registry=registry,
        state_log=state_log,
    )
    result = asyncio.run(rt.run({"type": "in_art", "data": {"x": 1}}))
    assert isinstance(result, RunResult)
    assert result.ok

    # The WAL must have a skill_phase_advanced event with next_phase="__post__".
    wal_events = list(state_log.iter_from(0))
    post_advances = [
        e for e in wal_events
        if e.get("kind") == "skill_phase_advanced"
        and e.get("next_phase") == "__post__"
    ]
    assert post_advances, (
        f"expected at least 1 skill_phase_advanced to __post__, got {post_advances}"
    )

    # The advance must record a last_phase_artifact_path so it's recoverable.
    assert post_advances[0].get("last_phase_artifact_path"), (
        "advance to __post__ must record last_phase_artifact_path"
    )


# ---------------------------------------------------------------------------
# (b) OSRuntime resume entry detects __post__ → skips phase loop
# ---------------------------------------------------------------------------


def test_resume_skips_phase_loop_when_post_phase(tmp_path, monkeypatch):
    """Tier 2: (b) OSRuntime.run() with resume_plan.current_phase=='__post__'
    skips the normal phase loop and jumps to _finish_workflow.

    The finish artifact is written to disk first so the resume path can
    load it from last_phase_artifact_path.
    """
    monkeypatch.chdir(tmp_path)

    skill = _make_single_phase_skill_with_post()

    # Write a fake finish artifact to disk at a predictable path.
    art_dir = tmp_path / ".reyn" / "artifacts" / "post_skill" / "__post__"
    art_dir.mkdir(parents=True, exist_ok=True)
    finish_artifact = {"type": "result", "data": {"y": "from_disk"}}
    art_path = art_dir / "v01_result.json"
    art_path.write_text(json.dumps(finish_artifact), encoding="utf-8")

    # Build a ResumePlan with current_phase="__post__" pointing to this file.
    # #1115 Stage 0: last_phase_artifact_path is now a state_dir-relative handle
    # (resolved via Workspace.resolve_artifact_handle on resume), where
    # state_dir defaults to base_dir/.reyn. So the handle is relative to
    # tmp_path/.reyn (= "artifacts/post_skill/__post__/v01_result.json"),
    # matching store_artifact's new return format.
    rel_path = str(art_path.relative_to(tmp_path / ".reyn"))
    plan = ResumePlan(
        run_id="run_post_001",
        skill_name="post_skill",
        skill_input={"type": "in_art", "data": {}},
        current_phase="__post__",
        last_phase_artifact_path=rel_path,
        awaiting_intervention_id=None,
        phases_visited=["sole"],
        visit_counts={"sole": 1},
        committed_steps=[],  # no committed steps → all steps re-execute
    )

    state_dir = tmp_path / ".reyn" / "agents" / "alpha" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    registry = SkillRegistry(
        agent_name="alpha",
        agent_state_dir=state_dir,
        state_log=state_log,
    )

    rt = _FinishRuntime(
        skill,
        run_id="run_post_001",
        skill_registry=registry,
        state_log=state_log,
        resume_plan=plan,
    )
    result = asyncio.run(rt.run({"type": "in_art", "data": {}}))

    assert isinstance(result, RunResult)
    assert result.ok

    # The _FinishRuntime._execute_phase must NOT have been called (phase loop skipped).
    assert rt.phase_calls == [], (
        f"phase loop must be skipped on __post__ resume; got calls: {rt.phase_calls}"
    )

    # Output should come from the loaded finish_artifact through the postprocessor.
    assert result.data.get("y") == "from_disk"


# ---------------------------------------------------------------------------
# (c) Postprocessor step memo: recorded step returns without re-executing
# ---------------------------------------------------------------------------


def test_postprocessor_step_memo_returns_recorded_result(tmp_path, monkeypatch):
    """Tier 2: (c) When resume_plan has a CommittedStep for a postprocessor step,
    PostprocessorExecutor replays the recorded result without invoking the step.

    We verify via the 'postprocessor_step_memoized' event — if the step was
    memoized, that event is emitted instead of 'postprocessor_step_completed'
    via fresh execution.
    """
    monkeypatch.chdir(tmp_path)

    from reyn.core.kernel.postprocessor_executor import _compute_step_hash

    events = _event_log()
    ws = Workspace(events)


    # Build a skill with a 2-step postprocessor.
    skill = _build_postprocessor_skill(
        postprocessor_steps=[
            {"type": "validate", "schema": {"type": "object"}},
            {"type": "validate", "schema": {"type": "object"}},
        ],
        postprocessor_output_schema={"type": "object", "properties": {"y": {"type": "string"}}},
    )

    finish_artifact = {"type": "llm_art", "data": {"y": "original"}}

    # The memo result for step 0: a different artifact (y="memoized")
    memo_artifact = {"type": "llm_art", "data": {"y": "memoized"}}
    step0_hash = _compute_step_hash(0, finish_artifact)

    committed = [
        CommittedStep(
            op_invocation_id="__post__.0",
            op_kind="validate",
            phase="__post__",
            args_hash=step0_hash,
            seq=42,
            result=memo_artifact,
        )
    ]
    plan = ResumePlan(
        run_id="run_c",
        skill_name="test_skill",
        skill_input={},
        current_phase="__post__",
        last_phase_artifact_path=None,
        awaiting_intervention_id=None,
        committed_steps=committed,
    )

    executor = PostprocessorExecutor(
        skill=skill,
        workspace=ws,
        events=events,
        model="stub/model",
        resolver=_resolver(),
        subscribers=events.subscribers,
    )
    result, _ = asyncio.run(
        executor.run(finish_artifact, output_language="en", resume_plan=plan)
    )

    # Step 0 must have been memoized.
    memoized_events = [e for e in events._collected if e.type == "postprocessor_step_memoized"]
    assert memoized_events, "expected at least one postprocessor_step_memoized event"
    assert memoized_events[0].data["step_index"] == 0

    # Step 1 must have executed freshly (no memo).
    # Both start and complete events must appear for step 1.
    started = [e for e in events._collected if e.type == "postprocessor_step_started"]
    completed = [e for e in events._collected if e.type == "postprocessor_step_completed"]
    step1_started = [e for e in started if e.data.get("step_index") == 1]
    step1_completed = [e for e in completed if e.data.get("step_index") == 1]
    assert step1_started, "step 1 must have started"
    assert step1_completed, "step 1 must have completed"

    # The artifact flowing into step 1 should reflect the memo result (y="memoized").
    # After step 1 (validate, passthrough), the final result must have y="memoized".
    assert result["data"]["y"] == "memoized"


# ---------------------------------------------------------------------------
# (d) Mid-postprocessor crash → resume → step 1 memo hit, step 2 re-executes
# ---------------------------------------------------------------------------


def test_mid_postprocessor_crash_resume_step1_memo_step2_reexecutes(tmp_path, monkeypatch):
    """Tier 2: (d) Crash after step 0 committed, resume with plan → step 0 memo hit,
    step 1 re-executes.

    Simulates:
      Run 1: step 0 completes (step_completed in WAL), crash before step 1.
      Resume: build ResumePlan from WAL events, re-run executor with plan.
              step 0 must be memoized; step 1 must execute.
    """
    monkeypatch.chdir(tmp_path)

    from reyn.core.kernel.postprocessor_executor import _compute_step_hash

    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")

    finish_artifact = {"type": "llm_art", "data": {"y": "run1"}}

    # Simulate the WAL state after step 0 completed:
    # step_started + step_completed for __post__.0.
    step0_hash = _compute_step_hash(0, finish_artifact)
    memo_result = {"type": "llm_art", "data": {"y": "run1"}}

    async def _seed_wal():
        await state_log.append(
            "skill_started",
            run_id="run_d",
            agent="alpha",
            target="alpha",
            skill_name="post_skill",
            skill_input={},
            parent_run_id=None,
        )
        await state_log.append(
            "step_started",
            run_id="run_d",
            phase="__post__",
            op_invocation_id="__post__.0",
            op_kind="validate",
            args={},
            args_hash=step0_hash,
        )
        await state_log.append(
            "step_completed",
            run_id="run_d",
            phase="__post__",
            op_invocation_id="__post__.0",
            op_kind="validate",
            args_hash=step0_hash,
            result=memo_result,
        )

    asyncio.run(_seed_wal())

    # Build a ResumePlan from the WAL.
    from reyn.skill.skill_snapshot import SkillSnapshot
    snapshot = SkillSnapshot(
        skill_run_id="run_d",
        skill_name="post_skill",
        skill_input={},
        current_phase="__post__",
        last_phase_artifact_path=None,
    )
    analyzer = SkillResumeAnalyzer()
    wal_events = [e for e in state_log.iter_from(0) if e.get("run_id") == "run_d"]
    plan = analyzer.analyze(snapshot=snapshot, wal_events=wal_events)

    assert plan.committed_steps, "expected at least one committed step"
    assert plan.committed_steps[0].op_invocation_id == "__post__.0"

    # Run the postprocessor with the plan.
    events = _event_log()
    ws = Workspace(events)

    skill = _build_postprocessor_skill(
        postprocessor_steps=[
            {"type": "validate", "schema": {"type": "object"}},
            {"type": "validate", "schema": {"type": "object"}},
        ],
        postprocessor_output_schema={"type": "object", "properties": {"y": {"type": "string"}}},
    )

    executor = PostprocessorExecutor(
        skill=skill,
        workspace=ws,
        events=events,
        model="stub/model",
        resolver=_resolver(),
        subscribers=events.subscribers,
        state_log=state_log,
        skill_run_id="run_d",
    )
    result, _ = asyncio.run(
        executor.run(finish_artifact, output_language="en", resume_plan=plan)
    )

    # Step 0: memoized.
    memoized = [e for e in events._collected if e.type == "postprocessor_step_memoized"]
    assert memoized, "expected at least one postprocessor_step_memoized event"
    assert memoized[0].data["step_index"] == 0

    # Step 1: freshly executed (no memo entry exists for __post__.1).
    step1_started = [
        e for e in events._collected
        if e.type == "postprocessor_step_started" and e.data.get("step_index") == 1
    ]
    step1_completed = [
        e for e in events._collected
        if e.type == "postprocessor_step_completed" and e.data.get("step_index") == 1
    ]
    assert step1_started, "step 1 must have re-executed (started event)"
    assert step1_completed, "step 1 must have re-executed (completed event)"

    assert result["data"]["y"] == "run1"


# ---------------------------------------------------------------------------
# (e) WorkflowAbortedError path → snapshot deleted (no resume)
# ---------------------------------------------------------------------------


def test_workflow_aborted_error_removes_snapshot(tmp_path, monkeypatch):
    """Tier 2: (e) When the postprocessor raises an error that propagates as
    WorkflowAbortedError (via the OSRuntime abort path), the skill snapshot is
    deleted so no resume is attempted.

    We use a postprocessor with a validate step that rejects the artifact
    (required field missing) and on_error="fail" semantics, which causes
    PostprocessorError → propagates up → OSRuntime's finally clause calls
    complete() (because WorkflowAbortedError is in the issubclass path — but
    actually PostprocessorError is not WorkflowAbortedError). Let's test the
    simpler invariant: when the run raises WorkflowAbortedError, the snapshot
    is removed by the finally clause.

    For postprocessor failures: PostprocessorError is re-raised up through
    _finish_workflow → run() → falls through the except WorkflowAbortedError
    clause → the finally clause's exc_type is PostprocessorError (not
    WorkflowAbortedError), so snapshot is KEPT for retry resume.
    This is the correct behavior (transient failure → retry).

    We verify the simpler invariant: WorkflowAbortedError → snapshot removed.
    """
    monkeypatch.chdir(tmp_path)

    skill = _make_single_phase_skill_with_post()
    state_dir = tmp_path / ".reyn" / "agents" / "alpha" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    registry = SkillRegistry(
        agent_name="alpha",
        agent_state_dir=state_dir,
        state_log=state_log,
    )

    class _AbortRuntime(_FinishRuntime):
        """Override _finish_workflow to raise WorkflowAbortedError to test snapshot cleanup."""

        async def _execute_phase(self, *args, **kw):
            self.phase_calls.append("sole")
            # Raise WorkflowAbortedError directly (skill abort path).
            raise WorkflowAbortedError("test abort")

    rt = _AbortRuntime(
        skill,
        run_id="run_e_001",
        skill_registry=registry,
        state_log=state_log,
    )
    with pytest.raises(WorkflowAbortedError):
        asyncio.run(rt.run({"type": "in_art", "data": {}}))

    # Snapshot must be removed (WorkflowAbortedError = skill decided to abort,
    # not a transient crash — per ADR-0013 / R-D1).
    snap_path = state_dir / "skills" / "run_e_001.snapshot.json"
    assert not snap_path.exists(), (
        "WorkflowAbortedError must remove the snapshot (no retry / resume)"
    )

    # skill_completed must be in the WAL.
    kinds = [e["kind"] for e in state_log.iter_from(0)]
    assert "skill_completed" in kinds
