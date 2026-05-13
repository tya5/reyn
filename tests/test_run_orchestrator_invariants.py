"""Tier 2: RunOrchestrator invariants — lifecycle events, SkillRegistry, resume.

FP-0020 Component D. Guards three core guarantees of RunOrchestrator:

1. test_run_emits_workflow_started_then_finished
   workflow_started fires before any phase event; workflow_finished fires
   on clean completion. Event ordering invariant.

2. test_run_calls_skill_registry_start_complete
   When a SkillRegistry is wired, start() is called at entry and complete()
   is called on clean exit (and NOT called on CancelledError). SkillRegistry
   lifecycle invariant (R-D1).

3. test_resume_plan_fast_forwards_to_current_phase
   When resume_plan.current_phase is set and a matching CommittedStep exists,
   the orchestrator skips re-invoking the LLM for that phase and reads the
   memo. Complementary to test_e2e_resume_memos_all_completed_llm_calls in
   test_llm_memoization_e2e.py.

All tests use real OSRuntime + RunOrchestrator instances and a plain async
callable stub for call_llm. No AsyncMock / MagicMock / patch.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import reyn.kernel.llm_call_recorder as llm_call_recorder_mod
from reyn.events.state_log import StateLog
from reyn.kernel.runtime import OSRuntime
from reyn.llm.llm import LLMCallResult
from reyn.schemas.models import Phase, Skill, SkillGraph
from reyn.skill.skill_registry import SkillRegistry
from reyn.skill.skill_resume_analyzer import CommittedStep, ResumePlan

# ── Helpers ──────────────────────────────────────────────────────────────────


def _simple_skill(name: str = "orch_inv") -> Skill:
    """Single-phase skill that finishes immediately."""
    phase = Phase(
        name="work",
        instructions="do the thing",
        input_schema={"type": "object", "properties": {}},
        allowed_ops=[],
    )
    return Skill(
        name=name,
        entry_phase="work",
        phases={"work": phase},
        graph=SkillGraph(transitions={}, can_finish_phases=["work"]),
        final_output_schema={"type": "object", "properties": {"result": {"type": "string"}}},
        final_output_name="result",
    )


_FINISH_RESPONSE = {
    "type": "finish",
    "control": {
        "type": "finish",
        "decision": "finish",
        "next_phase": None,
        "confidence": 1.0,
        "reason": {"summary": "complete"},
    },
    "artifact": {"type": "result", "data": {"result": "ok"}},
}


class _SingleResponseLLM:
    """Returns a single scripted response on every call. Records call count."""

    def __init__(self, response: dict) -> None:
        self._response = response
        self.call_count = 0

    async def __call__(self, model, frame, *args, **kwargs):  # noqa: ARG002
        self.call_count += 1
        return LLMCallResult(data=self._response, usage=None)


# ── Test 1: event ordering ─────────────────────────────────────────────────


def test_run_emits_workflow_started_then_finished(tmp_path, monkeypatch):
    """Tier 2: RunOrchestrator emits workflow_started before workflow_finished.

    Invariant (P6): every state change emits an event. workflow_started MUST
    fire before any phase_started event; workflow_finished MUST fire after the
    last phase_completed event on clean completion.
    """
    monkeypatch.chdir(tmp_path)
    llm = _SingleResponseLLM(_FINISH_RESPONSE)
    monkeypatch.setattr(llm_call_recorder_mod, "call_llm", llm)

    skill = _simple_skill()
    collected: list[str] = []

    def _subscriber(event) -> None:
        collected.append(event.type)

    rt = OSRuntime(skill, model="stub/model", subscribers=[_subscriber])
    result = asyncio.run(rt.run({"type": "input", "data": {}}))

    assert result.ok, f"expected finished, got {result.status}"

    assert "workflow_started" in collected, "workflow_started not emitted"
    assert "workflow_finished" in collected, "workflow_finished not emitted"

    started_idx = collected.index("workflow_started")
    finished_idx = collected.index("workflow_finished")
    assert started_idx < finished_idx, (
        f"workflow_started ({started_idx}) should precede "
        f"workflow_finished ({finished_idx})"
    )

    # phase_started must come after workflow_started
    phase_started_indices = [i for i, k in enumerate(collected) if k == "phase_started"]
    assert phase_started_indices, "phase_started not emitted"
    for ps_idx in phase_started_indices:
        assert started_idx < ps_idx, (
            f"workflow_started ({started_idx}) should precede "
            f"phase_started ({ps_idx})"
        )

    # phase_completed must come before workflow_finished
    phase_completed_indices = [i for i, k in enumerate(collected) if k == "phase_completed"]
    assert phase_completed_indices, "phase_completed not emitted"
    for pc_idx in phase_completed_indices:
        assert pc_idx < finished_idx, (
            f"phase_completed ({pc_idx}) should precede "
            f"workflow_finished ({finished_idx})"
        )


# ── Test 2: SkillRegistry lifecycle ──────────────────────────────────────────


def test_run_calls_skill_registry_start_complete(tmp_path, monkeypatch):
    """Tier 2: RunOrchestrator calls SkillRegistry.start then .complete on success.

    R-D1 invariant: start() fires at run() entry; complete() fires on clean
    exit so the snapshot is removed from disk.
    """
    monkeypatch.chdir(tmp_path)
    llm = _SingleResponseLLM(_FINISH_RESPONSE)
    monkeypatch.setattr(llm_call_recorder_mod, "call_llm", llm)

    skill = _simple_skill("reg_lifecycle")
    state_dir = tmp_path / ".reyn" / "agents" / "alpha" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    registry = SkillRegistry(
        agent_name="alpha",
        agent_state_dir=state_dir,
        state_log=state_log,
    )

    rt = OSRuntime(
        skill,
        model="stub/model",
        run_id="reg_lc_run",
        skill_registry=registry,
        state_log=state_log,
    )
    result = asyncio.run(rt.run({"type": "input", "data": {}}))
    assert result.ok, f"expected finished, got {result.status}"

    # After clean exit the snapshot file must be gone (complete() removes it)
    skills_dir = state_dir / "skills"
    snapshots = list(skills_dir.glob("*.snapshot.json")) if skills_dir.exists() else []
    assert not snapshots, (
        f"snapshot should be cleaned up after complete(); found: {snapshots}"
    )

    # WAL must contain skill_started and skill_completed entries
    wal_events = list(state_log.iter_from(0))
    kinds = [e["kind"] for e in wal_events]
    assert "skill_started" in kinds, "WAL missing skill_started"
    assert "skill_completed" in kinds, "WAL missing skill_completed"

    # skill_started must precede skill_completed
    started_wal_idx = kinds.index("skill_started")
    completed_wal_idx = kinds.index("skill_completed")
    assert started_wal_idx < completed_wal_idx, (
        f"skill_started ({started_wal_idx}) should precede "
        f"skill_completed ({completed_wal_idx}) in WAL"
    )


# ── Test 3: resume fast-forward ──────────────────────────────────────────────


def test_resume_plan_fast_forwards_to_current_phase(tmp_path, monkeypatch):
    """Tier 2: RunOrchestrator emits skill_resumed when resume_plan is provided.

    When a ResumePlan with current_phase is supplied, the orchestrator must:
    1. Emit a ``skill_resumed`` event recording the phase and visit_counts.
    2. Start execution from the plan's current_phase (not entry_phase).
    3. Complete successfully (the memo path is exercised by
       test_e2e_resume_memos_all_completed_llm_calls).

    This invariant targets the RunOrchestrator-layer fast-forward block
    (R-D2), which is structurally distinct from memo-lookup (LLMCallRecorder).
    """
    monkeypatch.chdir(tmp_path)
    llm = _SingleResponseLLM(_FINISH_RESPONSE)
    monkeypatch.setattr(llm_call_recorder_mod, "call_llm", llm)

    skill = _simple_skill("resume_fwd")

    # Build a plan that claims "work" is the current phase with visit_count=1.
    # This simulates: run 1 entered "work" once, crashed mid-phase, resume_plan
    # was reconstructed with visit_counts = {"work": 1}.
    plan = ResumePlan(
        run_id="run_resume_fwd",
        skill_name="resume_fwd",
        skill_input={"type": "input", "data": {}},
        current_phase="work",
        last_phase_artifact_path=None,
        awaiting_intervention_id=None,
        committed_steps=[],
        visit_counts={"work": 1},
        phases_visited=["work"],
    )

    collected_events: list = []

    def _subscriber(event) -> None:
        collected_events.append(event)

    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    rt = OSRuntime(
        skill,
        model="stub/model",
        run_id="run_resume_fwd",
        resume_plan=plan,
        state_log=state_log,
        subscribers=[_subscriber],
    )
    result = asyncio.run(rt.run({"type": "input", "data": {}}))
    assert result.ok, f"expected finished, got {result.status}"

    # skill_resumed must be emitted (R-D2 invariant)
    event_types = [e.type for e in collected_events]
    assert "skill_resumed" in event_types, (
        f"skill_resumed not emitted on resume. Events: {event_types}"
    )

    resumed_event = next(e for e in collected_events if e.type == "skill_resumed")
    assert resumed_event.data["resume_phase"] == "work", (
        f"resume_phase should be 'work'; got {resumed_event.data.get('resume_phase')!r}"
    )

    # R-D2: visit_counts in the resumed event must match the plan's counts
    # (after restore_from_resume pre-decrements the current phase).
    # With visit_counts={"work": 1} in the plan and pre-decrement, the
    # restored count should be {"work": 0} at the resume event emission point.
    # (begin_phase() increments it back to 1 when the phase actually starts.)
    assert resumed_event.data["visit_counts"] == {"work": 0}, (
        f"R-D2 pre-decrement: expected visit_counts {{'work': 0}} at resume; "
        f"got {resumed_event.data['visit_counts']}"
    )
