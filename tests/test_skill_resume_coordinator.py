"""Tier 2: OS invariant — SkillResumeCoordinator maps active runs + policy → decisions.

The coordinator's contract is the seam between the storage layer
(SkillRegistry + StateLog), the analysis layer (SkillResumeAnalyzer),
and the policy layer (SkillResumeConfig). Misclassifications here flow
into runtime decisions that affect production data, so we pin every
policy → action mapping plus the discovery flow.

Observation: the ResumeDecision dataclass returned by
discover_and_decide / decide_for_plan. No mocks — real SkillRegistry
backed by tmp_path + real StateLog.
"""
from __future__ import annotations

import asyncio

from reyn.config import SkillResumeConfig
from reyn.events.state_log import StateLog
from reyn.skill.skill_registry import SkillRegistry
from reyn.skill.skill_resume_analyzer import (
    AmbiguousStep,
    ResumePlan,
)
from reyn.skill.skill_resume_coordinator import (
    ResumeDecision,
    SkillResumeCoordinator,
)

# ---------------------------------------------------------------------------
# decide_for_plan — policy → action mapping (pure-function tests)
# ---------------------------------------------------------------------------


def _plan(*, has_amb: bool, skill_name: str = "demo") -> ResumePlan:
    """Build a minimal ResumePlan with or without ambiguous steps."""
    amb = []
    if has_amb:
        amb.append(AmbiguousStep(
            op_invocation_id="draft.0",
            op_kind="mcp",
            phase="draft",
            args_hash="x",
            started_seq=10,
        ))
    return ResumePlan(
        run_id="r1",
        skill_name=skill_name,
        skill_input={},
        current_phase="draft",
        last_phase_artifact_path=None,
        awaiting_intervention_id=None,
        ambiguous_steps=amb,
    )


def test_no_ambiguity_yields_resume_action():
    """Tier 2: a plan without ambiguity → action='resume' regardless of policy."""
    coord = SkillResumeCoordinator()
    for policy_default in ("prompt", "retry", "skip", "discard_skill"):
        cfg = SkillResumeConfig(default=policy_default)
        d = coord.decide_for_plan(_plan(has_amb=False), cfg)
        assert d.action == "resume", f"policy={policy_default}"
        assert d.ambiguous_steps == []


def test_prompt_policy_with_ambiguity_yields_prompt_required():
    """Tier 2: default policy 'prompt' → action='prompt_required'."""
    coord = SkillResumeCoordinator()
    cfg = SkillResumeConfig(default="prompt")
    d = coord.decide_for_plan(_plan(has_amb=True), cfg)
    assert d.action == "prompt_required"
    assert d.ambiguous_steps, "ambiguous_steps must be populated for prompt_required"


def test_retry_policy_with_ambiguity_yields_retry():
    """Tier 2: default policy 'retry' → action='retry' (runtime drops memo for ambiguous steps)."""
    coord = SkillResumeCoordinator()
    cfg = SkillResumeConfig(default="retry")
    d = coord.decide_for_plan(_plan(has_amb=True), cfg)
    assert d.action == "retry"


def test_skip_policy_with_ambiguity_yields_skip():
    """Tier 2: default policy 'skip' → action='skip' (runtime synthesizes empty completion)."""
    coord = SkillResumeCoordinator()
    cfg = SkillResumeConfig(default="skip")
    d = coord.decide_for_plan(_plan(has_amb=True), cfg)
    assert d.action == "skip"


def test_discard_skill_policy_with_ambiguity_yields_discard():
    """Tier 2: default policy 'discard_skill' → action='discard' (runtime aborts the run)."""
    coord = SkillResumeCoordinator()
    cfg = SkillResumeConfig(default="discard_skill")
    d = coord.decide_for_plan(_plan(has_amb=True), cfg)
    assert d.action == "discard"


def test_per_skill_override_takes_precedence():
    """Tier 2: per_skill[name] overrides default for that skill."""
    coord = SkillResumeCoordinator()
    cfg = SkillResumeConfig(
        default="prompt",
        per_skill={"trusted_skill": "retry"},
    )
    d_trusted = coord.decide_for_plan(
        _plan(has_amb=True, skill_name="trusted_skill"), cfg,
    )
    d_other = coord.decide_for_plan(
        _plan(has_amb=True, skill_name="other"), cfg,
    )
    assert d_trusted.action == "retry"
    assert d_other.action == "prompt_required"


# ---------------------------------------------------------------------------
# discover_and_decide — full integration
# ---------------------------------------------------------------------------


def test_discover_and_decide_no_active_runs(tmp_path):
    """Tier 2: empty SkillRegistry → empty decisions list."""
    state_dir = tmp_path / ".reyn" / "agents" / "alpha" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    reg = SkillRegistry(
        agent_name="alpha", agent_state_dir=state_dir, state_log=log,
    )
    cfg = SkillResumeConfig()
    coord = SkillResumeCoordinator()
    decisions = coord.discover_and_decide(
        skill_registry=reg, state_log=log, policy=cfg,
    )
    assert decisions == []


def test_discover_and_decide_clean_run_yields_resume(tmp_path):
    """Tier 2: an active run with no ambiguous steps → action='resume'.

    Simulates the common case: skill ran cleanly, process restarts
    while skill is mid-flight (e.g. waiting for an LLM call), all
    previously-completed steps are paired.
    """
    state_dir = tmp_path / ".reyn" / "agents" / "alpha" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    reg = SkillRegistry(
        agent_name="alpha", agent_state_dir=state_dir, state_log=log,
    )

    async def setup():
        await reg.start(run_id="r1", skill_name="demo", skill_input={})
        await reg.advance_phase(run_id="r1", next_phase="draft")
        # Append a clean started/completed pair for this run
        await log.append("step_started", run_id="r1", op_invocation_id="draft.0",
                         op_kind="file", phase="draft", args_hash="abc",
                         args={"op": "write"})
        await log.append("step_completed", run_id="r1", op_invocation_id="draft.0",
                         op_kind="file", phase="draft", args_hash="abc",
                         result={"ok": True})

    asyncio.run(setup())

    # Simulate process restart with a fresh registry
    log2 = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    reg2 = SkillRegistry(
        agent_name="alpha", agent_state_dir=state_dir, state_log=log2,
    )
    coord = SkillResumeCoordinator()
    decisions = coord.discover_and_decide(
        skill_registry=reg2, state_log=log2, policy=SkillResumeConfig(),
    )
    assert decisions, "clean run must yield a resume decision"
    d = decisions[0]
    assert d.plan.run_id == "r1"
    assert d.action == "resume"
    assert d.ambiguous_steps == []
    # Committed step was paired
    assert d.plan.committed_steps, "committed_steps must be populated for clean run"


def test_discover_and_decide_ambiguous_step_applies_policy(tmp_path):
    """Tier 2: ambiguous step + retry policy → action='retry' with ambiguous list populated."""
    state_dir = tmp_path / ".reyn" / "agents" / "alpha" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    reg = SkillRegistry(
        agent_name="alpha", agent_state_dir=state_dir, state_log=log,
    )

    async def setup():
        await reg.start(run_id="r1", skill_name="trusted", skill_input={})
        await reg.advance_phase(run_id="r1", next_phase="draft")
        await log.append("step_started", run_id="r1", op_invocation_id="draft.0",
                         op_kind="mcp", phase="draft", args_hash="abc",
                         args={"tool": "create"})
        # No completion → ambiguous

    asyncio.run(setup())

    log2 = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    reg2 = SkillRegistry(
        agent_name="alpha", agent_state_dir=state_dir, state_log=log2,
    )
    cfg = SkillResumeConfig(per_skill={"trusted": "retry"})
    coord = SkillResumeCoordinator()
    decisions = coord.discover_and_decide(
        skill_registry=reg2, state_log=log2, policy=cfg,
    )
    assert decisions, "ambiguous run must yield a decision"
    d = decisions[0]
    assert d.action == "retry"
    assert d.ambiguous_steps, "ambiguous_steps must be populated for retry decision"
    assert d.ambiguous_steps[0].op_kind == "mcp"


def test_discover_does_not_cross_pair_runs(tmp_path):
    """Tier 2: two concurrent runs each get their own analysis — pairing is per-run.

    Critical invariant: a step_completed for run A must not pair with a
    step_started from run B even if op_invocation_ids collide.
    """
    state_dir = tmp_path / ".reyn" / "agents" / "alpha" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    reg = SkillRegistry(
        agent_name="alpha", agent_state_dir=state_dir, state_log=log,
    )

    async def setup():
        await reg.start(run_id="run_A", skill_name="demo", skill_input={})
        await reg.start(run_id="run_B", skill_name="demo", skill_input={})
        # Both runs use op_invocation_id "draft.0" but completion only
        # exists for run_A. run_B's step_started must remain ambiguous.
        await log.append("step_started", run_id="run_A", op_invocation_id="draft.0",
                         op_kind="file", phase="draft", args_hash="aa",
                         args={})
        await log.append("step_started", run_id="run_B", op_invocation_id="draft.0",
                         op_kind="mcp", phase="draft", args_hash="bb",
                         args={})
        await log.append("step_completed", run_id="run_A", op_invocation_id="draft.0",
                         op_kind="file", phase="draft", args_hash="aa",
                         result={})

    asyncio.run(setup())

    log2 = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    reg2 = SkillRegistry(
        agent_name="alpha", agent_state_dir=state_dir, state_log=log2,
    )
    coord = SkillResumeCoordinator()
    decisions = coord.discover_and_decide(
        skill_registry=reg2, state_log=log2, policy=SkillResumeConfig(),
    )

    by_run = {d.plan.run_id: d for d in decisions}
    # run_A: clean pair → resume, no ambiguity
    assert by_run["run_A"].action == "resume"
    assert by_run["run_A"].ambiguous_steps == []
    # run_B: orphan started → ambiguous → retry (default policy under
    # PR-resume-auto; auto-resume never blocks on prompt). Ambiguous
    # steps are surfaced for inspection regardless.
    assert by_run["run_B"].action == "retry"
    assert by_run["run_B"].ambiguous_steps, "run_B must have ambiguous steps from orphaned start"
    assert by_run["run_B"].ambiguous_steps[0].op_kind == "mcp"


# ---------------------------------------------------------------------------
# summarize — diagnostics
# ---------------------------------------------------------------------------


def test_summarize_counts_by_action():
    """Tier 2: summarize tallies the decision actions; useful for restart logging."""
    coord = SkillResumeCoordinator()
    decisions = [
        ResumeDecision(plan=_plan(has_amb=False), action="resume"),
        ResumeDecision(plan=_plan(has_amb=True), action="resume"),
        ResumeDecision(plan=_plan(has_amb=True), action="retry"),
        ResumeDecision(plan=_plan(has_amb=True), action="prompt_required"),
        ResumeDecision(plan=_plan(has_amb=True), action="prompt_required"),
    ]
    counts = coord.summarize(decisions)
    assert counts == {"resume": 2, "retry": 1, "prompt_required": 2}
