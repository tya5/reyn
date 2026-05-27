"""Tier 3 (e2e): PR-resume-auto end-to-end — crashed skill auto-resumes on restart.

Headline scenario:
  1. Run 1 of a 2-phase skill draft → review crashes mid-review (per
     ``test_resume_e2e._CrashAfterFirstPhase``).
  2. ChatSession is restored from disk (snapshot survives because the
     simulated crash bypassed the ``finally`` cleanup).
  3. ``_auto_resume_active_skills`` is called → discovers the in-flight
     run, applies default policy (retry), and invokes the launcher with
     the ResumeDecision.
  4. The launcher (test-injected) runs OSRuntime with the resume_plan
     → review phase finishes → skill_completed → per-skill snapshot
     unlinked.

This validates the chat-level auto-resume orchestration without a real
LLM (the OSRuntime is stubbed via a subclass that returns deterministic
phase decisions).

Reference: PR-resume-auto A6 in the active plan.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from reyn.chat.session import ChatSession
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
from reyn.skill.skill_resume_coordinator import ResumeDecision
from reyn.skill.skill_snapshot import SkillSnapshot

_RUN_ID = "run_auto_resume_e2e"
_SKILL_NAME = "resume_demo"


# ---------------------------------------------------------------------------
# Fixture skill (mirrors test_resume_e2e._make_two_phase_skill)
# ---------------------------------------------------------------------------


def _make_skill() -> Skill:
    draft = Phase(
        name="draft", instructions="d",
        input_schema={"type": "object", "properties": {}}, allowed_ops=[],
    )
    review = Phase(
        name="review", instructions="r",
        input_schema={"type": "object", "properties": {}}, allowed_ops=[],
    )
    return Skill(
        name=_SKILL_NAME, entry_phase="draft",
        phases={"draft": draft, "review": review},
        graph=SkillGraph(
            transitions={"draft": ["review"]},
            can_finish_phases=["review"],
        ),
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


class _StubResumeRuntime(OSRuntime):
    """OSRuntime that finishes the resumed phase deterministically."""

    def __init__(
        self, skill: Skill, *,
        resume_plan: Any,
        skill_registry: SkillRegistry,
        state_log: StateLog,
    ) -> None:
        super().__init__(
            skill, model="stub/model", run_id=_RUN_ID,
            skill_registry=skill_registry,
            state_log=state_log,
            resume_plan=resume_plan,
        )
        self.executed_phases: list[str] = []

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
        self.executed_phases.append(current_phase)
        return _finish_decision(), _finish_output(), 0


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------


def test_e2e_chatsession_auto_resume_completes_crashed_skill(tmp_path: Path, monkeypatch):
    """Tier 3: crashed skill on disk → ChatSession.auto_resume → completes via launcher.

    The injected launcher mimics what production does (build OSRuntime,
    call run with resume_plan) but with a deterministic stub instead
    of a real LLM. Verifies that:
      - the auto-resume path finds the active run
      - the launcher receives the ResumeDecision with the right plan
      - OSRuntime completes the skill (skill_completed in WAL)
      - the per-skill snapshot is removed by SkillRegistry.complete
    """
    monkeypatch.chdir(tmp_path)

    # Pre-existing crashed state on disk.
    state_dir = tmp_path / ".reyn" / "agents" / "alpha" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    skills_dir = state_dir / "skills"
    skills_dir.mkdir(exist_ok=True)
    snap = SkillSnapshot(
        skill_run_id=_RUN_ID,
        skill_name=_SKILL_NAME,
        skill_input={"type": "input", "data": {}},
        applied_seq=10,
        last_phase_applied_seq=10,
        current_phase="review",
        last_phase_artifact_path=None,
        history=["draft", "review"],
        visit_counts={"draft": 1, "review": 1},
    )
    snap_path = skills_dir / f"{_RUN_ID}.snapshot.json"
    snap.save(snap_path)

    # ChatSession with WAL configured (the SkillRegistry derives its
    # state_dir from agent_name, which matches our seed above).
    session = ChatSession(
        agent_name="alpha",
        state_log=StateLog(tmp_path / ".reyn" / "wal.jsonl"),
        snapshot_path=tmp_path / "alpha_snapshot.json",
    )

    # Launcher mimics _spawn_resumed_skill but without dsl skill loading
    # (we already have the Skill in-memory).
    launched_decisions: list[ResumeDecision] = []

    async def stub_launcher(decision: ResumeDecision) -> None:
        launched_decisions.append(decision)
        rt = _StubResumeRuntime(
            _make_skill(),
            resume_plan=decision.plan,
            skill_registry=session.get_skill_registry(),
            state_log=session._state_log,
        )
        result = await rt.run(decision.plan.skill_input)
        assert isinstance(result, RunResult)
        assert result.ok, f"expected finished, got {result.status}"
        # Critical: only the in-flight phase ran (draft was fast-forwarded)
        assert rt.executed_phases == ["review"]

    async def go():
        return await session._auto_resume_active_skills(
            launcher=stub_launcher,
        )

    decisions = asyncio.run(go())

    # Auto-resume found the active run, launched it with the right plan
    assert decisions[0].plan.run_id == _RUN_ID
    assert decisions[0].plan.skill_name == _SKILL_NAME
    assert decisions[0].plan.current_phase == "review"

    # Skill completed → WAL has skill_completed, snapshot removed.
    log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    kinds = [e["kind"] for e in log.iter_from(0)]
    assert "skill_completed" in kinds
    assert not snap_path.exists()
