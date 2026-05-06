"""Tier 3 (e2e): R-D2 LLM memoization end-to-end on resume.

Pins the user-facing guarantee: when a skill resumes after crash,
LLM calls that the original run completed are NOT re-invoked. This is
the cost duplication fix that motivated R-D2.

Scenario:

  Run 1 (no resume):
    - Phase ``draft`` completes via 3 LLM calls (act_0, act_1, finish)
    - Each call lands a ``step_completed`` (op_kind="llm") in the WAL
    - Skill finishes normally; per-skill snapshot is removed by complete()

  Simulated crash:
    - We re-create the per-skill snapshot manually to simulate
      `kill -9` having happened mid-phase (parallel to test_resume_e2e.py
      pattern — the synthetic-exception path is too forgiving because of
      OSRuntime's finally complete()).

  Run 2 (resume):
    - SkillResumeCoordinator builds a ResumePlan from snapshot + WAL
    - Plan contains the 3 LLM CommittedSteps from run 1
    - OSRuntime(resume_plan=plan) runs phase ``draft`` again
    - All 3 LLM calls memo-hit → recorded responses substituted
    - litellm stub call count = 0 across run 2

A regression here means resume re-pays LLM cost for previously-completed
calls — exactly what R-D2 is designed to prevent.
"""
from __future__ import annotations

import asyncio

import reyn.kernel.runtime as runtime_mod
from reyn.config import SkillResumeConfig
from reyn.events.state_log import StateLog
from reyn.kernel.runtime import OSRuntime
from reyn.llm.llm import LLMCallResult
from reyn.schemas.models import (
    Phase,
    Skill,
    SkillGraph,
)
from reyn.skill.skill_registry import SkillRegistry
from reyn.skill.skill_resume_coordinator import SkillResumeCoordinator
from reyn.skill.skill_snapshot import SkillSnapshot

# ---------------------------------------------------------------------------
# Skill + LLM stub
# ---------------------------------------------------------------------------


def _three_call_skill() -> Skill:
    """Single-phase skill that finishes after 3 LLM calls (act, act, finish).

    max_act_turns = 5 leaves headroom; the LLM stub controls the actual
    sequence via its scripted responses.
    """
    draft = Phase(
        name="draft",
        instructions="produce result",
        input_schema={"type": "object", "properties": {}},
        allowed_ops=[],
        max_act_turns=5,
    )
    return Skill(
        name="memo_e2e",
        entry_phase="draft",
        phases={"draft": draft},
        graph=SkillGraph(transitions={}, can_finish_phases=["draft"]),
        final_output_schema={"type": "object", "properties": {}},
        final_output_name="result",
    )


# Scripted LLM responses for run 1. Each call gets the next scripted reply.
# The runtime drives _run_act_loop: act-turn responses keep looping; the
# decide-turn response (type=finish) exits.
_SCRIPT = [
    # call 0: act turn (no ops, just continue) — simplest valid act response
    {
        "type": "act",
        "control": {
            "type": "transition", "decision": "continue",
            "next_phase": "draft", "confidence": 1.0,
            "reason": {"summary": "act 0"},
        },
        "ops": [],
    },
    # call 1: act turn
    {
        "type": "act",
        "control": {
            "type": "transition", "decision": "continue",
            "next_phase": "draft", "confidence": 1.0,
            "reason": {"summary": "act 1"},
        },
        "ops": [],
    },
    # call 2: finish — exits the act loop and gets validated
    {
        "type": "finish",
        "control": {
            "type": "finish", "decision": "finish", "next_phase": None,
            "confidence": 1.0, "reason": {"summary": "done"},
        },
        "artifact": {"type": "result", "data": {"call": 2}},
    },
]


class _ScriptedLLM:
    """Replays scripted responses; counts invocations.

    Used as a monkeypatch target for ``reyn.kernel.runtime.call_llm``.
    """

    def __init__(self, script: list[dict]) -> None:
        self._script = script
        self.call_count = 0

    async def __call__(self, model, frame, *args, **kwargs):  # noqa: ARG002
        idx = self.call_count
        self.call_count += 1
        if idx >= len(self._script):
            raise RuntimeError(
                f"LLM stub script exhausted (call {idx}, "
                f"{len(self._script)} scripted)",
            )
        return LLMCallResult(data=self._script[idx], usage=None)


# ---------------------------------------------------------------------------
# E2E test
# ---------------------------------------------------------------------------


def test_e2e_resume_memos_all_completed_llm_calls(tmp_path, monkeypatch):
    """Tier 3: phase had N LLM calls before crash → resume re-invokes 0.

    The cost duplication invariant. Drives _run_act_loop end-to-end so
    we exercise the real `_call_llm_and_record` memo path (not a stub).
    """
    monkeypatch.chdir(tmp_path)

    skill = _three_call_skill()
    state_dir = tmp_path / ".reyn" / "agents" / "alpha" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    registry = SkillRegistry(
        agent_name="alpha",
        agent_state_dir=state_dir,
        state_log=state_log,
    )

    # ── Run 1: complete normally with 3 LLM calls ─────────────────────
    llm = _ScriptedLLM(_SCRIPT)
    monkeypatch.setattr(runtime_mod, "call_llm", llm)

    rt1 = OSRuntime(
        skill,
        model="stub/model",
        run_id="run_e2e_memo",
        skill_registry=registry,
        state_log=state_log,
    )
    result1 = asyncio.run(rt1.run({"type": "input", "data": {}}))
    assert result1.ok, f"run 1 should complete; got {result1.status}"
    assert llm.call_count == 3, (
        f"run 1 should invoke LLM 3 times; got {llm.call_count}"
    )

    # WAL must contain 3 step_completed entries with op_kind="llm"
    wal_events = list(state_log.iter_from(0))
    llm_completes = [
        e for e in wal_events
        if e["kind"] == "step_completed" and e.get("op_kind") == "llm"
    ]
    assert len(llm_completes) == 3, (
        f"expected 3 LLM step_completed in WAL; got {len(llm_completes)}\n"
        f"WAL kinds: {[e['kind'] for e in wal_events]}"
    )

    # ── Simulate kill -9: re-create per-skill snapshot ────────────────
    # OSRuntime.complete() removed the snapshot. Manually re-create it as
    # if the process was killed while phase 'draft' was still in flight.
    surviving_snap = SkillSnapshot(
        skill_run_id="run_e2e_memo",
        skill_name="memo_e2e",
        skill_input={"type": "input", "data": {}},
        applied_seq=10,
        last_phase_applied_seq=10,
        current_phase="draft",
        last_phase_artifact_path=None,
        history=["draft"],
        visit_counts={"draft": 1},
    )
    snap_path = state_dir / "skills" / "run_e2e_memo.snapshot.json"
    snap_path.parent.mkdir(parents=True, exist_ok=True)
    surviving_snap.save(snap_path)

    # ── Run 2: resume, expect LLM call count = 0 ──────────────────────
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
    assert len(decisions) == 1, f"expected 1 active run; got {decisions}"
    decision = decisions[0]
    assert decision.action == "resume"
    # Plan must contain the 3 LLM committed steps
    llm_committed = [
        s for s in decision.plan.committed_steps if s.op_kind == "llm"
    ]
    assert len(llm_committed) == 3, (
        f"expected 3 LLM CommittedSteps in plan; got {len(llm_committed)}"
    )

    llm2 = _ScriptedLLM(_SCRIPT)  # fresh counter for run 2
    monkeypatch.setattr(runtime_mod, "call_llm", llm2)

    rt2 = OSRuntime(
        skill,
        model="stub/model",
        run_id="run_e2e_memo",
        skill_registry=registry2,
        state_log=state_log2,
        resume_plan=decision.plan,
    )
    result2 = asyncio.run(rt2.run({"type": "input", "data": {}}))

    assert result2.ok, f"resume should complete; got {result2.status}"
    # The headline assertion: zero LLM calls on resume.
    assert llm2.call_count == 0, (
        f"resume must memo-hit all 3 LLM calls; got {llm2.call_count} fresh "
        f"invocations (cost duplication regression)"
    )

    # step_memoized fired 3 times in run 2's events
    step_memoized = [e for e in rt2.events.all() if e.type == "step_memoized"]
    llm_memoed = [e for e in step_memoized if e.data.get("op_kind") == "llm"]
    assert len(llm_memoed) == 3, (
        f"expected 3 step_memoized events with op_kind=llm; got {len(llm_memoed)}"
    )
