"""Tier 2: OS invariant — OSRuntime fast-forwards through completed phases on resume.

When a ResumePlan is supplied to OSRuntime, run() must:
  1. Skip the entry phase loop and start at ``resume_plan.current_phase``
     (the phase that was in flight at crash time).
  2. Restore ``_visit_counts`` and ``_history`` from the plan so that
     loop-limit checks and transition logging continue from where the
     prior run left off.
  3. Thread the plan into ControlIRExecutor so step memoization is
     reachable during phase execution.
  4. resume_plan=None → entry-phase start, fresh state (backward compat).

Tests use the same _StubRuntime / SpyRegistry pattern as the existing
runtime integration tests, so we don't need a real LLM.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

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
from reyn.skill.skill_resume_analyzer import (
    CommittedStep,
    ResumePlan,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _two_phase_skill() -> Skill:
    """Two-phase skill: draft → review (finish-allowed). Allows transition tests."""
    draft = Phase(
        name="draft",
        instructions="draft phase",
        input_schema={"type": "object", "properties": {}},
        allowed_ops=[],
    )
    review = Phase(
        name="review",
        instructions="review phase",
        input_schema={"type": "object", "properties": {}},
        allowed_ops=[],
    )
    return Skill(
        name="two_phase",
        entry_phase="draft",
        phases={"draft": draft, "review": review},
        graph=SkillGraph(
            transitions={"draft": ["review"]},
            can_finish_phases=["review"],
        ),
        final_output_schema={"type": "object", "properties": {}},
        final_output_name="result",
    )


def _finish_decision() -> NormalizationResult:
    ctrl = ControlDecision(
        type="finish", decision="finish", next_phase=None,
        confidence=1.0, reason=ControlReason(summary="done"),
    )
    return NormalizationResult(control=ctrl)


def _finish_output() -> LLMOutput:
    ctrl = ControlDecision(
        type="finish", decision="finish", next_phase=None,
        confidence=1.0, reason=ControlReason(summary="done"),
    )
    return LLMOutput(
        control=ctrl, artifact={"type": "result", "data": {}}, ops=[],
    )


@dataclass
class _PhaseEntered:
    phase: str
    visit_counts_at_entry: dict[str, int] = field(default_factory=dict)
    history_at_entry: list[str] = field(default_factory=list)


class _StubRuntime(OSRuntime):
    """OSRuntime subclass that finishes immediately on first phase execution.

    Records per-phase entry state for assertion. No LLM is invoked.
    """

    def __init__(
        self,
        skill: Skill,
        *,
        resume_plan: ResumePlan | None = None,
    ) -> None:
        super().__init__(
            skill, model="stub/model", run_id="run_test_001",
            resume_plan=resume_plan,
        )
        self.entered_phases: list[_PhaseEntered] = []

    async def _enter_phase(self, phase_name: str, artifact: dict) -> None:
        # FP-0005: _enter_phase is now async; subclass must mirror.
        # Capture state BEFORE the parent updates visit_counts so we
        # can verify fast-forward initialization.
        self.entered_phases.append(_PhaseEntered(
            phase=phase_name,
            visit_counts_at_entry=dict(self._visit_counts),
            history_at_entry=list(self._history),
        ))
        await super()._enter_phase(phase_name, artifact)

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
        return _finish_decision(), _finish_output(), 0


def _plan(
    *,
    current_phase: str = "review",
    visit_counts: dict[str, int] | None = None,
    phases_visited: list[str] | None = None,
    last_artifact_path: str | None = None,
    committed: list[CommittedStep] | None = None,
) -> ResumePlan:
    return ResumePlan(
        run_id="run_test_001",
        skill_name="two_phase",
        skill_input={"type": "input", "data": {}},
        current_phase=current_phase,
        last_phase_artifact_path=last_artifact_path,
        awaiting_intervention_id=None,
        phases_visited=phases_visited or ["draft"],
        visit_counts=visit_counts or {"draft": 1},
        committed_steps=committed or [],
    )


# ---------------------------------------------------------------------------
# Tests — fast-forward behavior
# ---------------------------------------------------------------------------


def test_resume_plan_starts_at_current_phase_not_entry():
    """Tier 2: with resume_plan, run() enters resume_plan.current_phase first (not the skill's entry_phase)."""
    skill = _two_phase_skill()
    plan = _plan(current_phase="review")
    rt = _StubRuntime(skill, resume_plan=plan)

    asyncio.run(rt.run({"type": "input", "data": {}}))

    assert len(rt.entered_phases) >= 1
    # Fast-forward: entry phase ('draft') is skipped; we start at 'review'.
    assert rt.entered_phases[0].phase == "review"


def test_resume_plan_restores_visit_counts():
    """Tier 2: visit_counts initialized from resume_plan, not blank.

    Without this, loop-limit detection would lose its prior state on
    resume — a phase that already ran 5 times under max=5 would get a
    fresh budget on resume and could run another 5 times.
    """
    skill = _two_phase_skill()
    plan = _plan(
        current_phase="review",
        visit_counts={"draft": 3},
        phases_visited=["draft", "draft", "draft"],
    )
    rt = _StubRuntime(skill, resume_plan=plan)

    asyncio.run(rt.run({"type": "input", "data": {}}))

    # The entry-time snapshot of visit_counts shows the restored draft=3
    # PLUS no review entry yet (review is what we're entering).
    first_entry = rt.entered_phases[0]
    assert first_entry.visit_counts_at_entry.get("draft") == 3


def test_resume_plan_restores_history():
    """Tier 2: _history initialized from phases_visited so transitions log correctly."""
    skill = _two_phase_skill()
    plan = _plan(
        current_phase="review",
        phases_visited=["draft"],
        visit_counts={"draft": 1},
    )
    rt = _StubRuntime(skill, resume_plan=plan)

    asyncio.run(rt.run({"type": "input", "data": {}}))

    first_entry = rt.entered_phases[0]
    assert first_entry.history_at_entry == ["draft"]


def test_resume_plan_none_starts_at_entry_phase():
    """Tier 2: backward compat — resume_plan=None starts at skill.entry_phase with empty state."""
    skill = _two_phase_skill()
    rt = _StubRuntime(skill, resume_plan=None)

    asyncio.run(rt.run({"type": "input", "data": {}}))

    assert len(rt.entered_phases) >= 1
    # No fast-forward: we start at the skill's declared entry_phase
    assert rt.entered_phases[0].phase == "draft"
    # Empty state at first entry
    assert rt.entered_phases[0].visit_counts_at_entry == {}
    assert rt.entered_phases[0].history_at_entry == []


def test_resume_plan_threads_into_control_ir_executor():
    """Tier 2: OSRuntime hands resume_plan to ControlIRExecutor on construction.

    Verified by inspecting executor._resume_plan after OSRuntime init.
    Without this wiring, dispatch_tool memoization (D3b-1) would never
    trigger during a real resume, even though ResumePlan exists.
    """
    skill = _two_phase_skill()
    plan = _plan()
    rt = _StubRuntime(skill, resume_plan=plan)

    # ControlIRExecutor was constructed in OSRuntime.__init__
    assert rt.control_ir_executor._resume_plan is plan


def test_resume_plan_completes_normally_after_fast_forward():
    """Tier 2: end-to-end smoke — fast-forward + finish runs to completion.

    Catches subtle breakage where fast-forward state corruption
    causes the runtime to error instead of finishing.
    """
    skill = _two_phase_skill()
    plan = _plan(current_phase="review")
    rt = _StubRuntime(skill, resume_plan=plan)

    result = asyncio.run(rt.run({"type": "input", "data": {}}))

    assert isinstance(result, RunResult)
    assert result.ok, f"expected finished, got {result.status}"


def test_resume_plan_uses_last_phase_artifact_when_provided(tmp_path, monkeypatch):
    """Tier 2: when resume_plan.last_phase_artifact_path is set, the current_phase enters with that artifact loaded as input.

    The pre-crash phase already wrote its output artifact. Resume must
    NOT replay that phase — the new current_phase needs the prior
    phase's artifact as its input.
    """
    monkeypatch.chdir(tmp_path)
    # Create a workspace artifact file at the path referenced in the plan
    artifact_dir = tmp_path / ".reyn" / "artifacts" / "two_phase" / "draft"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    artifact_file = artifact_dir / "v1_artifact.json"
    import json
    artifact_file.write_text(
        json.dumps({"type": "input", "data": {"recovered": True}}),
        encoding="utf-8",
    )

    skill = _two_phase_skill()
    plan = _plan(
        current_phase="review",
        last_artifact_path=str(artifact_file.relative_to(tmp_path)),
    )
    rt = _StubRuntime(skill, resume_plan=plan)

    # Smoke: resume completes; explicit artifact restoration semantics
    # (which pieces of the plan flow into _enter_phase's artifact) are
    # exercised by the e2e test in D3b-4.
    result = asyncio.run(rt.run({"type": "input", "data": {}}))
    assert result.ok
