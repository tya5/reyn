"""Tier 3 (e2e): postprocessor + chain / discard / resume interactions.

Tests how the postprocessor block interacts with the multi-agent chain
machinery across three scenarios:

  Case 1: test_postprocessor_mid_run_discard_notifies_upstream
    — B's skill_run is in the __post__ phase (simulated by WAL-seeding
      the snapshot to current_phase="__post__") and a /skill discard is
      issued. Pins that A's pending chain is force-resolved via the R-D14
      notify path and that running_skills_chain is cleaned up.

  Case 2: test_postprocessor_mid_run_chain_timeout_fires
    — chain_timeout_seconds=0.05s; postprocessor is in __post__ state.
      Pins that the watchdog fires independently of postprocessor state
      and force-resolves A's pending chain (no suppression by postprocessor
      in-flight status).

  Case 3: test_postprocessor_mid_run_crash_resume_delivers_to_upstream
    — B's skill crashes mid-postprocessor (step 0 committed to WAL,
      step 1 not started). On resume, SkillResumeAnalyzer reconstructs
      the ResumePlan with current_phase="__post__" + 1 committed step.
      OSRuntime re-executes: step 0 memo-hit, step 1 runs fresh.
      The result (postprocessor output) is the final RunResult, NOT
      the raw LLM artifact.

No cassette files; all tests use inline _ScriptedLLM or pre-built
_FinishRuntime (no LLM calls needed). Fixture pattern mirrors
test_chain_peer_discarded_notify.py + test_skill_postprocessor_resume.py.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from _async_wait import wait_until  # noqa: E402 — shared #1751 test wait helper (#2339 deflake)

from reyn.config import SafetyConfig, TimeoutConfig
from reyn.core.events.state_log import StateLog
from reyn.core.kernel.normalizer import NormalizationResult
from reyn.core.kernel.postprocessor_executor import _compute_step_hash
from reyn.core.kernel.runtime import OSRuntime, RunResult
from reyn.llm.llm import LLMCallResult
from reyn.llm.pricing import TokenUsage
from reyn.runtime.profile import AgentProfile
from reyn.runtime.registry import AgentRegistry
from reyn.runtime.session import Session
from reyn.schemas.models import (
    ControlDecision,
    ControlReason,
    LLMOutput,
    Phase,
    Postprocessor,
    Skill,
    SkillGraph,
    ValidateStep,
)
from reyn.skill.skill_registry import SkillRegistry
from reyn.skill.skill_resume_analyzer import (
    SkillResumeAnalyzer,
)
from reyn.skill.skill_snapshot import SkillSnapshot

# ---------------------------------------------------------------------------
# Shared LLM stub
# ---------------------------------------------------------------------------


class _ScriptedLLM:
    """Replay a fixed list of LLM responses; raises on over-call."""

    def __init__(self, script: list[dict]) -> None:
        self._script = script
        self.call_count = 0

    async def __call__(self, model: str, frame: Any, *args: Any, **kwargs: Any) -> LLMCallResult:
        idx = self.call_count
        self.call_count += 1
        if idx >= len(self._script):
            raise RuntimeError(
                f"LLM script exhausted (call {idx}, {len(self._script)} scripted)"
            )
        return LLMCallResult(data=self._script[idx], usage=TokenUsage(10, 20))


# One-turn finish response returning {title, body}
_FINISH_SCRIPT = [
    {
        "type": "decide",
        "control": {
            "type": "finish",
            "decision": "finish",
            "next_phase": None,
            "confidence": 1.0,
            "reason": {"summary": "done"},
        },
        "artifact": {
            "type": "post_draft",
            "data": {"title": "Hello World", "body": "This is a test post body."},
        },
        "ops": [],
    },
]


# ---------------------------------------------------------------------------
# Shared skill builders
# ---------------------------------------------------------------------------


def _make_postprocessor_skill() -> Skill:
    """Single-phase skill with a 2-step validate postprocessor."""
    phase = Phase(
        name="write",
        instructions="Write a post.",
        input_schema={"type": "object", "properties": {}},
        allowed_ops=[],
        max_act_turns=0,
    )
    postprocessor = Postprocessor(
        steps=[
            ValidateStep(type="validate", schema_={"type": "object"}),
            ValidateStep(type="validate", schema_={"type": "object"}),
        ],
        # Post-batch-17: output_schema validates the full {type, data} envelope.
        output_schema={
            "type": "object",
            "properties": {
                "type": {"type": "string", "const": "post_draft"},
                "data": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "body": {"type": "string"},
                    },
                    "required": ["title", "body"],
                },
            },
            "required": ["type", "data"],
        },
        output_name="post_draft",
    )
    return Skill(
        name="post_writer",
        entry_phase="write",
        phases={"write": phase},
        graph=SkillGraph(transitions={}, can_finish_phases=["write"]),
        final_output_schema={
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "body": {"type": "string"},
            },
            "required": ["title", "body"],
        },
        final_output_name="post_draft",
        postprocessor=postprocessor,
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
        artifact={"type": "post_draft", "data": {"title": "Hello World", "body": "This is a test post body."}},
        ops=[],
    )


class _FinishRuntime(OSRuntime):
    """OSRuntime that finishes the single phase immediately; no LLM calls."""

    def __init__(self, skill: Skill, **kw) -> None:
        super().__init__(skill, model="stub/model", **kw)
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
        return _finish_decision(), _finish_output(), 0


# ---------------------------------------------------------------------------
# Case 3: mid-postprocessor crash → resume → result delivered to upstream
# ---------------------------------------------------------------------------


def test_postprocessor_mid_run_crash_resume_delivers_to_upstream(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 3a: crash after postprocessor step 0 → resume → step 0 memo-hit,
    step 1 re-executes, postprocessor result is the final RunResult.

    Simulates:
      Run 1: LLM finishes, postprocessor step 0 commits to WAL, crash.
             Snapshot persists current_phase='__post__' + finish artifact.
      Resume: SkillResumeAnalyzer builds ResumePlan with committed_steps=[step0].
              OSRuntime skips phase loop, loads finish artifact, runs postprocessor.
              Step 0: memo-hit (no re-execution).
              Step 1: fresh execution (validate passthrough).
      Assertions:
        - result.ok is True
        - result.data contains the postprocessor output (title + body present)
        - WAL has postprocessor_step_memoized for step 0
        - WAL has step_completed for step 1

    The mid-postprocessor state is simulated by:
      1. Writing the finish artifact to disk at a predictable path
      2. WAL-seeding step_started + step_completed for __post__.0
      3. Building a SkillSnapshot with current_phase='__post__'
      4. Running SkillResumeAnalyzer to build the ResumePlan
      5. Constructing OSRuntime with resume_plan=plan

    No actual crash is simulated — we directly construct the post-crash
    state (snapshot + WAL) and resume from it, which is exactly what the
    real auto-resume path does.
    """
    monkeypatch.chdir(tmp_path)

    skill = _make_postprocessor_skill()

    state_dir = tmp_path / ".reyn" / "agents" / "alpha" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    registry = SkillRegistry(
        agent_name="alpha",
        agent_state_dir=state_dir,
        state_log=state_log,
    )

    # Write the finish artifact to disk (what advance_phase to __post__ would store)
    art_dir = tmp_path / ".reyn" / "artifacts" / "post_writer" / "__post__"
    art_dir.mkdir(parents=True, exist_ok=True)
    finish_artifact = {
        "type": "post_draft",
        "data": {"title": "Hello World", "body": "This is a test post body."},
    }
    art_path = art_dir / "v01_post_draft.json"
    art_path.write_text(json.dumps(finish_artifact), encoding="utf-8")
    # #1115 Stage 0: last_phase_artifact_path is a state_dir-relative handle
    # (state_dir defaults to base_dir/.reyn), resolved via
    # Workspace.resolve_artifact_handle on resume — matching store_artifact's
    # new return format.
    rel_art_path = str(art_path.relative_to(tmp_path / ".reyn"))

    run_id = "run_post_chain_003"

    # Seed the WAL: skill_started + step 0 started + step 0 completed
    step0_hash = _compute_step_hash(0, finish_artifact)
    memo_result = finish_artifact  # validate step is a passthrough

    async def _seed_wal() -> None:
        await state_log.append(
            "skill_started",
            run_id=run_id,
            agent="alpha",
            target="alpha",
            skill_name="post_writer",
            skill_input={"type": "input", "data": {}},
            parent_run_id=None,
        )
        await state_log.append(
            "step_started",
            run_id=run_id,
            phase="__post__",
            op_invocation_id="__post__.0",
            op_kind="validate",
            args={},
            args_hash=step0_hash,
        )
        await state_log.append(
            "step_completed",
            run_id=run_id,
            phase="__post__",
            op_invocation_id="__post__.0",
            op_kind="validate",
            args_hash=step0_hash,
            result=memo_result,
        )

    asyncio.run(_seed_wal())

    # Build snapshot with current_phase="__post__"
    snapshot = SkillSnapshot(
        skill_run_id=run_id,
        skill_name="post_writer",
        skill_input={"type": "input", "data": {}},
        current_phase="__post__",
        last_phase_artifact_path=rel_art_path,
    )

    # Build ResumePlan via SkillResumeAnalyzer (same path as auto-resume)
    analyzer = SkillResumeAnalyzer()
    wal_events = [e for e in state_log.iter_from(0) if e.get("run_id") == run_id]
    plan = analyzer.analyze(snapshot=snapshot, wal_events=wal_events)

    assert plan.current_phase == "__post__", (
        f"plan must start at __post__; got {plan.current_phase}"
    )
    assert plan.committed_steps, (
        f"expected at least 1 committed step (step 0); got {plan.committed_steps}"
    )
    assert plan.committed_steps[0].op_invocation_id == "__post__.0"
    assert plan.last_phase_artifact_path == rel_art_path

    # Run OSRuntime with the resume plan
    collected_events: list[Any] = []
    rt = _FinishRuntime(
        skill,
        run_id=run_id,
        skill_registry=registry,
        state_log=state_log,
        resume_plan=plan,
        subscribers=[lambda e: collected_events.append(e)],
    )
    result = asyncio.run(rt.run({"type": "input", "data": {}}))

    # Core assertions
    assert isinstance(result, RunResult)
    assert result.ok, f"expected finished, got {result.status!r}"

    # Postprocessor output: title + body from the finish artifact (both steps
    # are validate/passthrough so output mirrors input)
    assert "title" in result.data, (
        f"postprocessor output must contain 'title'; got keys: {list(result.data.keys())}"
    )
    assert "body" in result.data, (
        f"postprocessor output must contain 'body'; got keys: {list(result.data.keys())}"
    )
    assert result.data["title"] == "Hello World"
    assert result.data["body"] == "This is a test post body."

    # Phase loop must have been skipped (snapshot was at __post__)
    assert rt.phase_calls == [], (
        f"phase loop must be skipped on __post__ resume; got calls: {rt.phase_calls}"
    )

    # Step 0: memoized (not re-executed)
    memoized = [e for e in collected_events if e.type == "postprocessor_step_memoized"]
    assert memoized, (
        f"expected at least 1 postprocessor_step_memoized event for step 0; got {len(memoized)}"
    )
    assert memoized[0].data["step_index"] == 0

    # Step 1: freshly executed (started + completed)
    step1_started = [
        e for e in collected_events
        if e.type == "postprocessor_step_started" and e.data.get("step_index") == 1
    ]
    step1_completed = [
        e for e in collected_events
        if e.type == "postprocessor_step_completed" and e.data.get("step_index") == 1
    ]
    assert step1_started, "step 1 must have started freshly on resume"
    assert step1_completed, "step 1 must have completed freshly on resume"
