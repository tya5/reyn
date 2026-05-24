"""Tier 2: PlanResumeCoordinator decisions + apply (ADR-0023 §3.3 Phase 2 step 7c).

Pins:
  - decide_for_plan: all-complete → resume; default → retry_pending; discard config → discard
  - discover_and_decide: missing/corrupt artifact → forced discard
  - apply_decisions: cancels flagged children, calls plan_registry.complete on discard
  - build_plan_resume_config: yaml schema parsing with aliases + invalid-value fallback
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.chat.planner import Plan, PlanStep
from reyn.plan import (
    PlanRegistry,
    PlanResumeConfig,
    PlanResumeCoordinator,
    PlanResumeDecision,
    PlanResumePlan,
    PlanStepState,
    build_plan_resume_config,
    write_decomposition,
)


def _decomp() -> Plan:
    return Plan(
        goal="g",
        steps=(PlanStep("s1", "first", ()), PlanStep("s2", "second", ())),
    )


# ── build_plan_resume_config ──────────────────────────────────────────────


def test_build_config_default_when_raw_none() -> None:
    """Tier 2: missing reyn.yaml block yields default config."""
    cfg = build_plan_resume_config(None)
    assert cfg.default == "retry_pending"


def test_build_config_accepts_documented_aliases() -> None:
    """Tier 2: yaml accepts retry_pending_steps / discard_plan synonyms,
    coordinator normalizes internally."""
    cfg1 = build_plan_resume_config({"default": "retry_pending_steps"})
    assert cfg1.default == "retry_pending"
    cfg2 = build_plan_resume_config({"default": "discard_plan"})
    assert cfg2.default == "discard"


def test_build_config_invalid_top_level_falls_back_to_default() -> None:
    """Tier 2: unknown ``default`` value logs warning + falls back to
    builtin default (= mirror _build_skill_resume_config)."""
    cfg = build_plan_resume_config({"default": "invalid_x"})
    assert cfg.default == "retry_pending"


def test_build_config_child_purity_partial_override() -> None:
    """Tier 2: known purity keys override; unknown keys / invalid actions
    are logged + skipped, leaving the rest at their defaults."""
    cfg = build_plan_resume_config({
        "child_purity": {
            "pure": "adopt",            # override
            "unknown_key": "adopt",      # skipped
            "world": "garbage",          # invalid action — skipped
        }
    })
    assert cfg.child_purity["pure"] == "adopt"
    assert cfg.child_purity["world"] == "adopt"  # default unchanged


# ── decide_for_plan ───────────────────────────────────────────────────────


def test_decide_returns_resume_when_all_steps_complete() -> None:
    """Tier 2: every step in completed_with_result → action=resume (=
    finalize aggregation, no re-execute)."""
    rp = PlanResumePlan(
        plan_id="p001", chain_id="c0", goal="g",
        n_steps=2, decomposition_artifact_path=None,
        step_states=(
            PlanStepState(step_id="s1", state="completed_with_result", result_text="r1"),
            PlanStepState(step_id="s2", state="completed_with_result", result_text="r2"),
        ),
    )
    coord = PlanResumeCoordinator()
    decision = coord.decide_for_plan(rp)
    assert decision.action == "resume"


def test_decide_returns_retry_pending_when_default_policy() -> None:
    """Tier 2: default policy ``retry_pending`` produces that action with
    pending_step_ids carrying the unfinished steps."""
    rp = PlanResumePlan(
        plan_id="p001", chain_id="c0", goal="g",
        n_steps=2, decomposition_artifact_path=None,
        step_states=(
            PlanStepState(step_id="s1", state="completed_with_result", result_text="r1"),
            PlanStepState(step_id="s2", state="pending"),
        ),
    )
    coord = PlanResumeCoordinator()
    decision = coord.decide_for_plan(rp)
    assert decision.action == "retry_pending"
    assert decision.pending_step_ids == ("s2",)


def test_decide_returns_discard_when_config_says_discard() -> None:
    """Tier 2: PlanResumeConfig(default="discard") → coordinator chooses
    discard for any not-fully-complete plan."""
    rp = PlanResumePlan(
        plan_id="p001", chain_id="c0", goal="g",
        n_steps=2, decomposition_artifact_path=None,
        step_states=(
            PlanStepState(step_id="s1", state="completed_with_result", result_text="r1"),
            PlanStepState(step_id="s2", state="pending"),
        ),
    )
    coord = PlanResumeCoordinator(config=PlanResumeConfig(default="discard"))
    decision = coord.decide_for_plan(rp)
    assert decision.action == "discard"


def test_decide_default_adopts_in_flight_children() -> None:
    """Tier 2: retry_pending policy adopts in-flight children (= leave
    for skill auto-resume to recover; coordinator doesn't cancel them)."""
    rp = PlanResumePlan(
        plan_id="p001", chain_id="c0", goal="g",
        n_steps=2, decomposition_artifact_path=None,
        step_states=(
            PlanStepState(
                step_id="s1", state="interrupted_with_child",
                child_run_id="child_xyz", child_state="in_flight",
            ),
            PlanStepState(step_id="s2", state="pending"),
        ),
    )
    coord = PlanResumeCoordinator()
    decision = coord.decide_for_plan(rp)
    assert decision.action == "retry_pending"
    assert decision.child_actions == {"child_xyz": "adopt"}


def test_decide_discard_cancels_children() -> None:
    """Tier 2: discard policy cancels every child via
    SkillRegistry.complete(status="discarded")."""
    rp = PlanResumePlan(
        plan_id="p001", chain_id="c0", goal="g",
        n_steps=2, decomposition_artifact_path=None,
        step_states=(
            PlanStepState(
                step_id="s1", state="interrupted_with_child",
                child_run_id="child_xyz", child_state="in_flight",
            ),
            PlanStepState(step_id="s2", state="pending"),
        ),
    )
    coord = PlanResumeCoordinator(config=PlanResumeConfig(default="discard"))
    decision = coord.decide_for_plan(rp)
    assert decision.action == "discard"
    assert decision.child_actions == {"child_xyz": "cancel"}


# ── discover_and_decide ───────────────────────────────────────────────────


def test_discover_forces_discard_on_missing_artifact(tmp_path: Path) -> None:
    """Tier 2: missing decomposition artifact → forced discard with
    descriptive plan stub."""
    reg = PlanRegistry(agent_name="default", agent_state_dir=tmp_path)
    reg.start(plan_id="p001", chain_id="c0", goal="g", applied_seq=10)

    def loader(_plan_id: str) -> Plan:
        raise FileNotFoundError(_plan_id)

    coord = PlanResumeCoordinator()
    decisions = coord.discover_and_decide(
        plan_registry=reg, wal_events=[], decomposition_loader=loader,
    )
    assert decisions and decisions[0].action == "discard"


def test_discover_forces_discard_on_corrupt_artifact(tmp_path: Path) -> None:
    """Tier 2: artifact load raising any exception → forced discard."""
    from reyn.plan.decomposition import DecompositionCorruptError

    reg = PlanRegistry(agent_name="default", agent_state_dir=tmp_path)
    reg.start(plan_id="p001", chain_id="c0", goal="g", applied_seq=10)

    def loader(_plan_id: str) -> Plan:
        raise DecompositionCorruptError("schema drift")

    coord = PlanResumeCoordinator()
    decisions = coord.discover_and_decide(
        plan_registry=reg, wal_events=[], decomposition_loader=loader,
    )
    assert decisions[0].action == "discard"


def test_discover_normal_path_produces_decisions(tmp_path: Path) -> None:
    """Tier 2: when artifact loads, analyzer runs and decision reflects state."""
    reg = PlanRegistry(agent_name="default", agent_state_dir=tmp_path)
    snap = reg.start(plan_id="p001", chain_id="c0", goal="g", applied_seq=10)
    snap.step_results["s1"] = "r1"  # mock pre-crash s1 completion
    plan = _decomp()

    events = [
        {"seq": 1, "kind": "plan_step_started", "plan_id": "p001", "step_id": "s1"},
        {"seq": 2, "kind": "plan_step_completed", "plan_id": "p001",
         "step_id": "s1", "content_len": 2},
    ]

    def loader(plan_id: str) -> Plan:
        return plan

    coord = PlanResumeCoordinator()
    decisions = coord.discover_and_decide(
        plan_registry=reg, wal_events=events, decomposition_loader=loader,
    )
    assert decisions
    decision = decisions[0]
    assert decision.action == "retry_pending"
    assert decision.pending_step_ids == ("s2",)


# ── apply_decisions ───────────────────────────────────────────────────────


class _MockSkillRegistry:
    def __init__(self) -> None:
        self.completed_runs: list[tuple[str, str]] = []

    async def complete(self, *, run_id: str, status: str = "completed") -> None:
        self.completed_runs.append((run_id, status))


@pytest.mark.asyncio
async def test_apply_returns_launchable_subset(tmp_path: Path) -> None:
    """Tier 2: apply_decisions returns plans for ChatSession to spawn
    (= action ∈ {resume, retry_pending}); discard plans are excluded."""
    reg = PlanRegistry(agent_name="default", agent_state_dir=tmp_path)
    reg.start(plan_id="p001", chain_id="c0", goal="g", applied_seq=10)
    reg.start(plan_id="p002", chain_id="c1", goal="g2", applied_seq=20)

    rp1 = PlanResumePlan(
        plan_id="p001", chain_id="c0", goal="g", n_steps=1,
        decomposition_artifact_path=None,
        step_states=(PlanStepState(step_id="s1", state="pending"),),
    )
    rp2 = PlanResumePlan(
        plan_id="p002", chain_id="c1", goal="g2", n_steps=0,
        decomposition_artifact_path=None,
    )
    decisions = [
        PlanResumeDecision(plan=rp1, action="retry_pending", pending_step_ids=("s1",)),
        PlanResumeDecision(plan=rp2, action="discard"),
    ]
    coord = PlanResumeCoordinator()
    launchable = await coord.apply_decisions(
        decisions, plan_registry=reg, skill_registry=None,
    )
    assert launchable and launchable[0].plan.plan_id == "p001"


@pytest.mark.asyncio
async def test_apply_discard_calls_plan_registry_complete(tmp_path: Path) -> None:
    """Tier 2: discard branch removes the plan via plan_registry.complete
    so the snapshot is cleaned up."""
    reg = PlanRegistry(agent_name="default", agent_state_dir=tmp_path)
    reg.start(plan_id="p002", chain_id="c1", goal="g2", applied_seq=20)
    rp = PlanResumePlan(
        plan_id="p002", chain_id="c1", goal="g2", n_steps=0,
        decomposition_artifact_path=None,
    )
    decisions = [
        PlanResumeDecision(plan=rp, action="discard"),
    ]
    coord = PlanResumeCoordinator()
    await coord.apply_decisions(
        decisions, plan_registry=reg, skill_registry=None,
    )
    assert reg.get("p002") is None


@pytest.mark.asyncio
async def test_apply_cancels_children_flagged_cancel(tmp_path: Path) -> None:
    """Tier 2: child_actions["cancel"] entries trigger
    skill_registry.complete(status="discarded") for each."""
    reg = PlanRegistry(agent_name="default", agent_state_dir=tmp_path)
    reg.start(plan_id="p001", chain_id="c0", goal="g", applied_seq=10)
    skill_reg = _MockSkillRegistry()

    rp = PlanResumePlan(
        plan_id="p001", chain_id="c0", goal="g", n_steps=1,
        decomposition_artifact_path=None,
        step_states=(
            PlanStepState(
                step_id="s1", state="interrupted_with_child",
                child_run_id="child_xyz", child_state="unknown",
            ),
        ),
    )
    decisions = [
        PlanResumeDecision(
            plan=rp, action="retry_pending", pending_step_ids=("s1",),
            child_actions={"child_xyz": "cancel"},
        ),
    ]
    coord = PlanResumeCoordinator()
    await coord.apply_decisions(
        decisions, plan_registry=reg, skill_registry=skill_reg,
    )
    assert ("child_xyz", "discarded") in skill_reg.completed_runs


@pytest.mark.asyncio
async def test_apply_adopt_leaves_children_alone(tmp_path: Path) -> None:
    """Tier 2: adopt children NOT cancelled — left for skill_resume infra."""
    reg = PlanRegistry(agent_name="default", agent_state_dir=tmp_path)
    reg.start(plan_id="p001", chain_id="c0", goal="g", applied_seq=10)
    skill_reg = _MockSkillRegistry()

    rp = PlanResumePlan(
        plan_id="p001", chain_id="c0", goal="g", n_steps=1,
        decomposition_artifact_path=None,
        step_states=(
            PlanStepState(
                step_id="s1", state="interrupted_with_child",
                child_run_id="child_xyz", child_state="in_flight",
            ),
        ),
    )
    decisions = [
        PlanResumeDecision(
            plan=rp, action="retry_pending", pending_step_ids=("s1",),
            child_actions={"child_xyz": "adopt"},
        ),
    ]
    coord = PlanResumeCoordinator()
    await coord.apply_decisions(
        decisions, plan_registry=reg, skill_registry=skill_reg,
    )
    assert skill_reg.completed_runs == []  # adopted, not cancelled


@pytest.mark.asyncio
async def test_apply_outbox_notice_fires_on_discard(tmp_path: Path) -> None:
    """Tier 2: discard branch surfaces an outbox notice for the user."""
    reg = PlanRegistry(agent_name="default", agent_state_dir=tmp_path)
    reg.start(plan_id="p002", chain_id="c1", goal="g2", applied_seq=20)
    notices: list[tuple[str, str]] = []

    async def on_outbox(plan_id: str, message: str) -> None:
        notices.append((plan_id, message))

    rp = PlanResumePlan(
        plan_id="p002", chain_id="c1", goal="g2", n_steps=0,
        decomposition_artifact_path=None,
    )
    decisions = [PlanResumeDecision(plan=rp, action="discard")]
    coord = PlanResumeCoordinator()
    await coord.apply_decisions(
        decisions, plan_registry=reg, skill_registry=None,
        on_outbox_notice=on_outbox,
    )
    assert notices and notices[0][0] == "p002"
    assert "discarded" in notices[0][1]
