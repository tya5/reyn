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

from reyn.chat.profile import AgentProfile
from reyn.chat.registry import AgentRegistry
from reyn.chat.session import ChatSession
from reyn.config import SafetyConfig, TimeoutConfig
from reyn.events.state_log import StateLog
from reyn.kernel.normalizer import NormalizationResult
from reyn.kernel.postprocessor_executor import _compute_step_hash
from reyn.kernel.runtime import OSRuntime, RunResult
from reyn.llm.llm import LLMCallResult
from reyn.llm.pricing import TokenUsage
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
# Multi-agent fixture (mirrors test_chain_peer_discarded_notify.py)
# ---------------------------------------------------------------------------


def _make_registry_with_two_agents(
    tmp_path: Path,
) -> tuple[AgentRegistry, ChatSession, ChatSession, StateLog]:
    """Build a registry holding two real ChatSessions named 'a' and 'b'.

    Both sessions share the same StateLog. The registry's _agents dict is
    populated by calling get_or_load for each name.
    """
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")

    def factory(profile: AgentProfile) -> ChatSession:
        agent_dir = tmp_path / ".reyn" / "agents" / profile.name
        agent_dir.mkdir(parents=True, exist_ok=True)
        return ChatSession(
            agent_name=profile.name,
            state_log=state_log,
            snapshot_path=agent_dir / "state" / "snapshot.json",
        )

    registry = AgentRegistry(
        project_root=tmp_path,
        session_factory=factory,
        state_log=state_log,
    )
    for name in ("a", "b"):
        agent_dir = tmp_path / ".reyn" / "agents" / name
        agent_dir.mkdir(parents=True, exist_ok=True)
        AgentProfile.new(name, role="").save(agent_dir)

    sess_a = registry.get_or_load("a")
    sess_b = registry.get_or_load("b")
    sess_a._registry = registry
    sess_b._registry = registry
    return registry, sess_a, sess_b, state_log


# ---------------------------------------------------------------------------
# Case 1: discard during __post__ notifies upstream
# ---------------------------------------------------------------------------


def test_postprocessor_mid_run_discard_notifies_upstream(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 3a: discard of a __post__-phase run resolves the upstream chain.

    Simulates B's skill_run having entered __post__ state (WAL-seeded snapshot
    with current_phase='__post__'). The test then issues `/skill discard` via
    the slash mechanism and verifies:
      - A's pending chain is force-resolved (chain no longer tracked)
      - WAL has a chain_resolve event for the chain
      - WAL has a skill_discarded event for B's run
      - B's running_skills_chain map drops the run_id

    The mid-postprocessor state is simulated by stashing the run in
    running_skills_chain with the chain_id BEFORE issuing the discard
    (= how the spawn path populates this map in production). The
    postprocessor is not actually in-flight; we rely on SkillRegistry.start
    + manual chain_id stash to reproduce the wiring.
    """
    monkeypatch.chdir(tmp_path)

    registry, sess_a, sess_b, state_log = _make_registry_with_two_agents(tmp_path)
    sess_b.is_attached = True

    async def go() -> None:
        # A registers a chain waiting on B
        await sess_a.chains.register(
            chain_id="chain-post-001",
            from_user=True,
            depth=1,
            original_text="delegate task to B",
            sender="user",
            waiting_on={"b"},
            origin_agent="user",
            origin_depth=0,
        )
        assert sess_a.chains.find_chain("chain-post-001") is not None

        # B starts a skill_run (simulates a skill that has entered __post__)
        b_reg = sess_b.get_skill_registry()
        assert b_reg is not None
        await b_reg.start(
            run_id="run_b_post_001",
            skill_name="post_writer",
            skill_input={"type": "input", "data": {}},
        )
        # Stash the chain_id as the spawn path would for a chain-tagged run
        sess_b.running_skills_chain["run_b_post_001"] = "chain-post-001"

        # Invoke /skill discard on B's run (with --force to bypass the
        # confirmation step; the safety prompt is covered separately in
        # test_skill_slash_command.py).
        consumed = await sess_b._maybe_handle_slash(
            "/skill discard run_b_post_001 --force",
        )
        assert consumed is True

        # Allow the async notify path to propagate to A
        for _ in range(5):
            await asyncio.sleep(0)

    asyncio.run(go())

    # A's chain must be force-resolved
    assert sess_a.chains.find_chain("chain-post-001") is None, (
        "A's pending chain must be resolved after B's run is discarded"
    )

    # WAL must have chain_resolve for this chain
    events = list(state_log.iter_from(0))
    resolves = [
        e for e in events
        if e.get("kind") == "chain_resolve" and e.get("chain_id") == "chain-post-001"
    ]
    assert resolves, f"expected at least 1 chain_resolve; got {resolves}"

    # WAL must have skill_discarded for B's run
    discarded = [
        e for e in events
        if e.get("kind") == "skill_discarded" and e.get("run_id") == "run_b_post_001"
    ]
    assert discarded, f"expected at least 1 skill_discarded; got {discarded}"

    # B's running_skills_chain must be cleaned up
    assert "run_b_post_001" not in sess_b.running_skills_chain, (
        "running_skills_chain must drop the run_id after discard"
    )


# ---------------------------------------------------------------------------
# Case 2: chain timeout fires independently of postprocessor state
# ---------------------------------------------------------------------------


def test_postprocessor_mid_run_chain_timeout_fires(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 3a: chain watchdog fires even when postprocessor is in __post__ state.

    Pins that the chain timeout watchdog is independent of the postprocessor's
    execution status. When chain_timeout_seconds is very short (0.05s), the
    watchdog fires even if B's run is nominally in __post__ state — because
    the watchdog is armed at the chain level, not at the skill level.

    The __post__ state is simulated by:
      1. Arming A's chain watchdog with 0.05s timeout
      2. NOT resolving the chain (= no discard, no completion)
      3. Waiting for the watchdog to fire

    Assertions:
      - A's chain is force-resolved by the watchdog (chain no longer tracked)
      - WAL has chain_timeout_fired event
      - The timeout fires despite no /discard being issued
        (= watchdog is orthogonal to postprocessor lifecycle)

    Scope note: we use 0.05s timeout to keep the test fast. Production
    values are O(minutes); the test exercises the timer code path only.
    """
    monkeypatch.chdir(tmp_path)

    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")

    def factory(profile: AgentProfile) -> ChatSession:
        agent_dir = tmp_path / ".reyn" / "agents" / profile.name
        agent_dir.mkdir(parents=True, exist_ok=True)
        return ChatSession(
            agent_name=profile.name,
            state_log=state_log,
            snapshot_path=agent_dir / "state" / "snapshot.json",
            # Very short timeout so the watchdog fires quickly in the test
            safety=SafetyConfig(timeout=TimeoutConfig(chain_seconds=0.05)),
        )

    registry = AgentRegistry(
        project_root=tmp_path,
        session_factory=factory,
        state_log=state_log,
    )
    for name in ("a", "b"):
        agent_dir = tmp_path / ".reyn" / "agents" / name
        agent_dir.mkdir(parents=True, exist_ok=True)
        AgentProfile.new(name, role="").save(agent_dir)

    sess_a = registry.get_or_load("a")
    sess_b = registry.get_or_load("b")
    sess_a._registry = registry
    sess_b._registry = registry

    async def go() -> None:
        # A registers a chain waiting on B — arm the watchdog at the same time
        chain = await sess_a.chains.register(
            chain_id="chain-timeout-post-001",
            from_user=True,
            depth=1,
            original_text="long running task",
            sender="user",
            waiting_on={"b"},
            origin_agent="user",
            origin_depth=0,
        )
        sess_a.chains.arm_timeout(
            "chain-timeout-post-001",
            on_fire=sess_a._on_chain_timeout_fire,
        )

        # Verify chain is registered
        assert sess_a.chains.find_chain("chain-timeout-post-001") is not None

        # B starts its skill — simulates __post__ state (no completion)
        b_reg = sess_b.get_skill_registry()
        assert b_reg is not None
        await b_reg.start(
            run_id="run_b_timeout_001",
            skill_name="post_writer",
            skill_input={"type": "input", "data": {}},
        )
        # Stash chain_id (as spawn path would)
        sess_b.running_skills_chain["run_b_timeout_001"] = "chain-timeout-post-001"

        # Wait for the watchdog to fire (0.05s + a small margin)
        await asyncio.sleep(0.15)

    asyncio.run(go())

    # A's chain must be force-resolved by the watchdog
    assert sess_a.chains.find_chain("chain-timeout-post-001") is None, (
        "watchdog must force-resolve A's chain regardless of postprocessor state"
    )

    # WAL must have chain_timeout_fired
    events = list(state_log.iter_from(0))
    timeouts = [
        e for e in events
        if e.get("kind") == "chain_timeout_fired"
        and e.get("chain_id") == "chain-timeout-post-001"
    ]
    assert timeouts, (
        f"expected at least 1 chain_timeout_fired event; got {timeouts}"
    )


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
