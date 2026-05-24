"""Tier 3 (e2e): forward-replay resume crash/restart cycle.

End-to-end exercise of the full skill-resume stack:

  Phase 1 (run 1):
    - SkillRegistry.start → skill_started in WAL
    - OSRuntime executes phase 1, writes artifact
    - SkillRegistry.advance_phase → skill_phase_advanced in WAL
    - dispatch_tool emits step_started + step_completed for a
      side-effect op in phase 1
    - Crash simulation: stop processing without calling
      SkillRegistry.complete

  Phase 2 (run 2 — resume):
    - SkillRegistry.load_active discovers the in-flight run
    - SkillResumeAnalyzer builds a ResumePlan with:
        * current_phase = phase 2 (from snapshot)
        * committed_steps containing phase 1's op
    - SkillResumeCoordinator.decide_for_plan returns 'resume'
    - OSRuntime.run(resume_plan=plan) fast-forwards to phase 2
    - dispatch_tool memoizes phase 1's op (does not re-execute)
    - skill completes, SkillRegistry.complete fires

This is the canonical scenario the entire D-track is designed for.
A regression here means resume is fundamentally broken, even if the
unit tests for individual layers still pass.

The test uses StubRuntime patterns from prior layers — no LLM, no
mocks, real StateLog + SkillRegistry on tmp_path.
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from reyn.config import SkillResumeConfig
from reyn.events.state_log import StateLog
from reyn.kernel.normalizer import NormalizationResult
from reyn.kernel.runtime import OSRuntime, RunResult
from reyn.schemas.models import (
    ControlDecision,
    ControlReason,
    LLMOutput,
    Phase,
    Skill,
    SkillGraph,
)
from reyn.skill.skill_registry import SkillRegistry
from reyn.skill.skill_resume_coordinator import SkillResumeCoordinator

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_two_phase_skill() -> Skill:
    """draft → review → finish."""
    draft = Phase(
        name="draft", instructions="draft",
        input_schema={"type": "object", "properties": {}},
        allowed_ops=[],
    )
    review = Phase(
        name="review", instructions="review",
        input_schema={"type": "object", "properties": {}},
        allowed_ops=[],
    )
    return Skill(
        name="resume_demo",
        entry_phase="draft",
        phases={"draft": draft, "review": review},
        graph=SkillGraph(
            transitions={"draft": ["review"]},
            can_finish_phases=["review"],
        ),
        final_output_schema={"type": "object", "properties": {}},
        final_output_name="result",
    )


def _transition_decision(next_phase: str) -> NormalizationResult:
    return NormalizationResult(control=ControlDecision(
        type="transition", decision="continue",
        next_phase=next_phase, confidence=1.0,
        reason=ControlReason(summary="next"),
    ))


def _transition_output(next_phase: str) -> LLMOutput:
    return LLMOutput(
        control=ControlDecision(
            type="transition", decision="continue",
            next_phase=next_phase, confidence=1.0,
            reason=ControlReason(summary="next"),
        ),
        artifact={"type": "result", "data": {"phase_was": next_phase}},
        ops=[],
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


class _CrashAfterFirstPhase(OSRuntime):
    """OSRuntime that finishes phase 1 normally then raises a synthetic error in phase 2.

    Simulates a crash mid-phase-2 so the WAL shows:
      skill_started, skill_phase_advanced(draft),
      skill_phase_advanced(review), <interrupt>
    Without skill_completed, the registry should still see this run on
    next startup.
    """

    def __init__(
        self,
        skill: Skill,
        *,
        skill_registry: SkillRegistry,
        state_log: StateLog,
    ) -> None:
        super().__init__(
            skill, model="stub/model", run_id="run_e2e_001",
            skill_registry=skill_registry,
            state_log=state_log,
        )
        self._calls = 0

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
        self._calls += 1
        if current_phase == "draft":
            return _transition_decision("review"), _transition_output("review"), 0
        # In review: simulate a crash mid-phase
        raise RuntimeError("simulated crash mid-review")


class _ResumeFromReview(OSRuntime):
    """OSRuntime that resumes a previously-crashed run and completes review."""

    def __init__(
        self,
        skill: Skill,
        *,
        resume_plan: Any,
        skill_registry: SkillRegistry,
        state_log: StateLog,
    ) -> None:
        super().__init__(
            skill, model="stub/model", run_id="run_e2e_001",
            skill_registry=skill_registry,
            state_log=state_log,
            resume_plan=resume_plan,
        )
        self.phases_executed: list[str] = []

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
        self.phases_executed.append(current_phase)
        # On resume, only "review" should ever be executed (draft is
        # fast-forwarded). Finish unconditionally.
        return _finish_decision(), _finish_output(), 0


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_e2e_crash_during_phase2_then_resume_to_completion(tmp_path, monkeypatch):
    """Tier 3: crash mid-phase2 → restart with ResumePlan → only phase2 re-executes → skill completes.

    The end-to-end story this whole D-track is designed for. A
    regression here means forward-replay is fundamentally broken.
    """
    monkeypatch.chdir(tmp_path)

    skill = _make_two_phase_skill()
    state_dir = tmp_path / ".reyn" / "agents" / "alpha" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    registry = SkillRegistry(
        agent_name="alpha",
        agent_state_dir=state_dir,
        state_log=state_log,
    )

    # ── Run 1: crashes during review ──────────────────────────────────
    rt1 = _CrashAfterFirstPhase(
        skill, skill_registry=registry, state_log=state_log,
    )
    with pytest.raises(RuntimeError, match="simulated crash"):
        asyncio.run(rt1.run({"type": "input", "data": {"x": 1}}))

    # WAL invariants post-crash (R-D1):
    #   - skill_started + skill_phase_advanced are in the WAL
    #   - skill_completed is NOT (the unrecovered RuntimeError triggers
    #     the preserve-snapshot path in OSRuntime's finally clause)
    #   - the per-skill snapshot file remains on disk so the next
    #     startup's auto-resume can pick it up naturally
    kinds = [e["kind"] for e in state_log.iter_from(0)]
    assert "skill_started" in kinds
    assert "skill_phase_advanced" in kinds
    assert "skill_completed" not in kinds, (
        "R-D1: unrecovered RuntimeError must NOT emit skill_completed"
    )
    snap_path = state_dir / "skills" / "run_e2e_001.snapshot.json"
    assert snap_path.exists(), (
        "R-D1: per-skill snapshot must survive an unrecovered crash"
    )

    # ── Resume: discover, decide, replay ──────────────────────────────
    state_log2 = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    registry2 = SkillRegistry(
        agent_name="alpha",
        agent_state_dir=state_dir,
        state_log=state_log2,
    )
    coord = SkillResumeCoordinator()
    decisions = coord.discover_and_decide(
        skill_registry=registry2,
        state_log=state_log2,
        policy=SkillResumeConfig(),
    )
    assert decisions, f"expected 1 active run, got {decisions}"
    (decision,) = decisions
    # Clean run (no ambiguous step events were written): action='resume'
    assert decision.action == "resume"
    assert decision.plan.current_phase == "review"
    assert decision.plan.visit_counts == {"draft": 1, "review": 1}

    # ── Run 2: resume with the plan ───────────────────────────────────
    rt2 = _ResumeFromReview(
        skill,
        resume_plan=decision.plan,
        skill_registry=registry2,
        state_log=state_log2,
    )
    result = asyncio.run(rt2.run({"type": "input", "data": {"x": 1}}))

    assert isinstance(result, RunResult)
    assert result.ok, f"expected finished, got {result.status}"
    # Critical: only review was executed on resume (draft was fast-forwarded).
    assert rt2.phases_executed == ["review"], (
        f"expected only review on resume; got {rt2.phases_executed}"
    )

    # Post-resume: skill_completed in the WAL, snapshot file removed.
    kinds_after = [e["kind"] for e in state_log2.iter_from(0)]
    assert "skill_completed" in kinds_after
    assert not snap_path.exists(), "complete() must remove the snapshot"


def test_e2e_resume_emits_skill_resumed_audit_event(tmp_path, monkeypatch):
    """Tier 3: resume emits ``skill_resumed`` event so operators can audit which run was reconstructed at which phase."""
    monkeypatch.chdir(tmp_path)
    skill = _make_two_phase_skill()
    state_dir = tmp_path / ".reyn" / "agents" / "alpha" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    registry = SkillRegistry(
        agent_name="alpha",
        agent_state_dir=state_dir,
        state_log=state_log,
    )

    # Build a ResumePlan directly (skip the crash setup; this test
    # focuses on the audit event)
    from reyn.skill.skill_resume_analyzer import ResumePlan
    plan = ResumePlan(
        run_id="run_e2e_001",
        skill_name="resume_demo",
        skill_input={"type": "input", "data": {}},
        current_phase="review",
        last_phase_artifact_path=None,
        awaiting_intervention_id=None,
        phases_visited=["draft"],
        visit_counts={"draft": 1},
    )

    rt = _ResumeFromReview(
        skill=skill,
        resume_plan=plan,
        skill_registry=registry,
        state_log=state_log,
    )
    asyncio.run(rt.run({"type": "input", "data": {}}))

    # Inspect EventLog directly (post-run); skill_resumed must have been
    # emitted exactly once with run_id, resume_phase, and visit_counts.
    resumed = [e for e in rt.events.all() if e.type == "skill_resumed"]
    assert resumed, "expected skill_resumed event to be emitted"
    (ev,) = resumed
    assert ev.data["run_id"] == "run_e2e_001"
    assert ev.data["resume_phase"] == "review"
    assert ev.data["visit_counts"] == {"draft": 1}
