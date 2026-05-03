"""Tier 2: OS invariant — SkillResumeCoordinator.apply_decisions.

Background: ``decide_for_plan`` returns ``ResumeDecision`` per active
skill run. The runtime layer (ChatSession startup) needs to consume
these decisions:
  - ``discard`` → call ``SkillRegistry.complete(status='discarded')``
    + drop pending interventions for the run_id
  - ``resume`` / ``retry`` / ``skip`` / ``prompt_required`` → caller
    launches the runtime with ``resume_plan=decision.plan``

This test pins ``apply_decisions`` which orchestrates the discard side
effects and returns the still-launchable decisions for the caller.

Reference: PR-resume-auto A1 in the active plan.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from reyn.config import SkillResumeConfig
from reyn.events.state_log import StateLog
from reyn.skill.skill_registry import SkillRegistry
from reyn.skill.skill_resume_analyzer import (
    AmbiguousStep,
    CommittedStep,
    ResumePlan,
)
from reyn.skill.skill_resume_coordinator import (
    ResumeDecision,
    SkillResumeCoordinator,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_registry(tmp_path: Path) -> tuple[SkillRegistry, StateLog]:
    state_dir = tmp_path / ".reyn" / "agents" / "alpha" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    log = StateLog(tmp_path / "wal.jsonl")
    return SkillRegistry(
        agent_name="alpha",
        agent_state_dir=state_dir,
        state_log=log,
    ), log


def _plan(*, run_id: str, has_ambiguity: bool = False) -> ResumePlan:
    return ResumePlan(
        run_id=run_id,
        skill_name="demo",
        skill_input={},
        current_phase="draft",
        last_phase_artifact_path=None,
        awaiting_intervention_id=None,
        committed_steps=[],
        ambiguous_steps=(
            [AmbiguousStep(
                op_invocation_id="draft.0",
                op_kind="file",
                phase="draft",
                args_hash="abc",
                started_seq=10,
            )]
            if has_ambiguity else []
        ),
    )


# ---------------------------------------------------------------------------
# discard action triggers SkillRegistry.complete(status='discarded')
# ---------------------------------------------------------------------------


def test_discard_action_calls_skill_registry_complete(tmp_path: Path):
    """Tier 2: ``discard`` decision → registry.complete(status='discarded') + WAL skill_discarded.

    Verified end-to-end via WAL inspection (not via mocking
    SkillRegistry, per testing policy).
    """
    registry, log = _make_registry(tmp_path)

    async def go():
        # Pre-register a run so the discard target exists
        await registry.start(
            run_id="run_to_discard",
            skill_name="demo",
            skill_input={"type": "input", "data": {}},
        )
        coord = SkillResumeCoordinator()
        decision = ResumeDecision(
            plan=_plan(run_id="run_to_discard"),
            action="discard",
        )
        remaining = await coord.apply_decisions(
            [decision], skill_registry=registry,
        )
        return remaining

    remaining = asyncio.run(go())

    # No remaining decisions for the caller to launch
    assert remaining == [], (
        f"discard removes the decision from the launch list; got {remaining}"
    )
    # WAL has skill_discarded event
    kinds = [e["kind"] for e in log.iter_from(0)]
    assert "skill_discarded" in kinds


def test_discard_action_drops_pending_interventions(tmp_path: Path):
    """Tier 2: ``discard`` calls the intervention-drop callable for the run_id."""
    registry, _ = _make_registry(tmp_path)
    dropped_runs: list[str] = []

    def fake_drop(run_id: str) -> None:
        dropped_runs.append(run_id)

    async def go():
        await registry.start(
            run_id="run_with_iv",
            skill_name="demo",
            skill_input={"type": "input", "data": {}},
        )
        coord = SkillResumeCoordinator()
        decision = ResumeDecision(
            plan=_plan(run_id="run_with_iv"),
            action="discard",
        )
        await coord.apply_decisions(
            [decision],
            skill_registry=registry,
            drop_interventions_for_run=fake_drop,
        )

    asyncio.run(go())
    assert dropped_runs == ["run_with_iv"], (
        f"intervention drop must fire for discarded run; got {dropped_runs}"
    )


def test_discard_works_without_intervention_drop_callable(tmp_path: Path):
    """Tier 2: drop callable is optional; absence does not crash."""
    registry, _ = _make_registry(tmp_path)

    async def go():
        await registry.start(
            run_id="run_no_iv",
            skill_name="demo",
            skill_input={"type": "input", "data": {}},
        )
        coord = SkillResumeCoordinator()
        decision = ResumeDecision(
            plan=_plan(run_id="run_no_iv"),
            action="discard",
        )
        return await coord.apply_decisions(
            [decision], skill_registry=registry,
        )

    remaining = asyncio.run(go())
    assert remaining == []


# ---------------------------------------------------------------------------
# Non-discard actions are passed through unchanged
# ---------------------------------------------------------------------------


def test_resume_action_passed_through(tmp_path: Path):
    """Tier 2: ``resume`` decision returned in remaining list (caller launches it)."""
    registry, _ = _make_registry(tmp_path)

    async def go():
        coord = SkillResumeCoordinator()
        d = ResumeDecision(
            plan=_plan(run_id="run_resume"), action="resume",
        )
        return await coord.apply_decisions([d], skill_registry=registry)

    remaining = asyncio.run(go())
    assert len(remaining) == 1
    assert remaining[0].action == "resume"


def test_retry_action_passed_through(tmp_path: Path):
    """Tier 2: ``retry`` action also passes through (= ambiguous step retried via empty memo)."""
    registry, _ = _make_registry(tmp_path)

    async def go():
        coord = SkillResumeCoordinator()
        d = ResumeDecision(
            plan=_plan(run_id="run_retry", has_ambiguity=True),
            action="retry",
        )
        return await coord.apply_decisions([d], skill_registry=registry)

    remaining = asyncio.run(go())
    assert len(remaining) == 1
    assert remaining[0].action == "retry"


def test_skip_action_passed_through(tmp_path: Path):
    """Tier 2: ``skip`` action passes through with synthetic CommittedSteps already injected by decide_for_plan."""
    registry, _ = _make_registry(tmp_path)

    async def go():
        coord = SkillResumeCoordinator()
        # Plan with skip-synthesized memo (= ambiguous → committed_steps)
        plan = ResumePlan(
            run_id="run_skip", skill_name="demo", skill_input={},
            current_phase="draft", last_phase_artifact_path=None,
            awaiting_intervention_id=None,
            committed_steps=[
                CommittedStep(
                    op_invocation_id="draft.0",
                    op_kind="file",
                    phase="draft",
                    args_hash="abc",
                    seq=10,
                    result={"status": "skipped"},
                ),
            ],
        )
        d = ResumeDecision(plan=plan, action="skip")
        return await coord.apply_decisions([d], skill_registry=registry)

    remaining = asyncio.run(go())
    assert len(remaining) == 1
    assert remaining[0].action == "skip"
    assert remaining[0].plan.committed_steps[0].result == {"status": "skipped"}


def test_prompt_required_treated_as_retry_for_auto_resume(tmp_path: Path):
    """Tier 2: ``prompt_required`` falls through as retry (R-D3 dropped, no interactive prompt).

    Under the auto-resume design (PR-resume-auto), the system never
    blocks on a prompt. If the operator explicitly sets
    ``skill_resume.default: prompt`` in reyn.yaml, the runtime treats
    it as ``retry`` (= ambiguous steps re-execute). Future PR may
    surface a warning, but blocking is unacceptable for auto-resume.
    """
    registry, _ = _make_registry(tmp_path)

    async def go():
        coord = SkillResumeCoordinator()
        d = ResumeDecision(
            plan=_plan(run_id="run_prompt", has_ambiguity=True),
            action="prompt_required",
        )
        return await coord.apply_decisions([d], skill_registry=registry)

    remaining = asyncio.run(go())
    # Remains in launchable list — the caller will launch it; ambiguous
    # steps are absent from committed_steps so they retry naturally.
    assert len(remaining) == 1
    assert remaining[0].action in ("prompt_required", "retry")


# ---------------------------------------------------------------------------
# Mixed batch
# ---------------------------------------------------------------------------


def test_apply_decisions_mixed_batch(tmp_path: Path):
    """Tier 2: discard + resume + retry batch → only resume + retry returned."""
    registry, log = _make_registry(tmp_path)

    async def go():
        await registry.start(
            run_id="run_a", skill_name="demo",
            skill_input={"type": "input", "data": {}},
        )
        await registry.start(
            run_id="run_b", skill_name="demo",
            skill_input={"type": "input", "data": {}},
        )
        await registry.start(
            run_id="run_c", skill_name="demo",
            skill_input={"type": "input", "data": {}},
        )
        coord = SkillResumeCoordinator()
        decisions = [
            ResumeDecision(plan=_plan(run_id="run_a"), action="resume"),
            ResumeDecision(plan=_plan(run_id="run_b"), action="discard"),
            ResumeDecision(plan=_plan(run_id="run_c"), action="retry"),
        ]
        return await coord.apply_decisions(
            decisions, skill_registry=registry,
        )

    remaining = asyncio.run(go())
    actions = [d.action for d in remaining]
    run_ids = [d.plan.run_id for d in remaining]
    assert sorted(run_ids) == ["run_a", "run_c"]
    assert "discard" not in actions
    # WAL has exactly one skill_discarded for run_b
    discarded = [
        e for e in log.iter_from(0)
        if e["kind"] == "skill_discarded"
    ]
    assert len(discarded) == 1
    assert discarded[0]["run_id"] == "run_b"
