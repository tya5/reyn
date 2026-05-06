"""Tier 2: PR-resume-ux U1 — skip action augments resume plan with empty-result memos.

When the resume policy is ``skip``, the Coordinator must inject synthetic
CommittedStep entries for each AmbiguousStep so that on resume the
dispatch_tool memo lookup hits and returns an empty ok result without
re-executing the (possibly already-committed) op.

Without this augmentation, action="skip" would have the same runtime
behavior as action="resume" — the ambiguous op would re-execute,
defeating the whole point of skip.
"""
from __future__ import annotations

from reyn.config import SkillResumeConfig
from reyn.dispatch.dispatcher import _compute_args_hash
from reyn.skill.skill_resume_analyzer import (
    AmbiguousStep,
    CommittedStep,
    ResumePlan,
)
from reyn.skill.skill_resume_coordinator import (
    SkillResumeCoordinator,
)


def _plan_with_ambiguous(*, op_invocation_id: str = "draft.0",
                         phase: str = "draft",
                         args_hash: str = "abc123") -> ResumePlan:
    return ResumePlan(
        run_id="run_skip",
        skill_name="demo",
        skill_input={},
        current_phase=phase,
        last_phase_artifact_path=None,
        awaiting_intervention_id=None,
        committed_steps=[],
        ambiguous_steps=[
            AmbiguousStep(
                op_invocation_id=op_invocation_id,
                op_kind="file",
                phase=phase,
                args_hash=args_hash,
                started_seq=10,
                args={"path": "out.txt", "content": "x"},
            ),
        ],
    )


def test_skip_policy_injects_committed_step_for_ambiguous(tmp_path):
    """Tier 2: action=skip → plan.committed_steps gains synthetic entries."""
    coord = SkillResumeCoordinator()
    plan = _plan_with_ambiguous()
    policy = SkillResumeConfig(default="skip")

    decision = coord.decide_for_plan(plan, policy)

    assert decision.action == "skip"
    matched = [
        s for s in decision.plan.committed_steps
        if s.op_invocation_id == "draft.0"
    ]
    assert len(matched) == 1, (
        "skip must inject a CommittedStep matching the AmbiguousStep's id; "
        f"got {decision.plan.committed_steps}"
    )
    synthetic = matched[0]
    assert synthetic.op_kind == "file"
    assert synthetic.phase == "draft"
    assert synthetic.args_hash == "abc123"
    # Empty result so dispatch_tool returns ok with empty data
    assert synthetic.result == {} or synthetic.result is None or \
        synthetic.result == {"status": "skipped"}, (
        f"skip's synthetic CommittedStep should carry an empty/skipped "
        f"result; got {synthetic.result}"
    )


def test_skip_policy_preserves_existing_committed_steps(tmp_path):
    """Tier 2: skip augments — does not replace already-committed steps."""
    coord = SkillResumeCoordinator()
    pre_existing = CommittedStep(
        op_invocation_id="draft.completed_op",
        op_kind="file",
        phase="draft",
        args_hash="prev",
        seq=5,
        result={"path": "ok.txt"},
    )
    plan = ResumePlan(
        run_id="run_skip",
        skill_name="demo",
        skill_input={},
        current_phase="draft",
        last_phase_artifact_path=None,
        awaiting_intervention_id=None,
        committed_steps=[pre_existing],
        ambiguous_steps=[
            AmbiguousStep(
                op_invocation_id="draft.ambig",
                op_kind="file", phase="draft", args_hash="amb",
                started_seq=10, args={},
            ),
        ],
    )

    decision = coord.decide_for_plan(plan, SkillResumeConfig(default="skip"))

    ids = [s.op_invocation_id for s in decision.plan.committed_steps]
    assert "draft.completed_op" in ids, (
        "pre-existing committed steps must be preserved"
    )
    assert "draft.ambig" in ids, (
        "skip must inject for ambiguous step"
    )


def test_non_skip_policy_does_not_augment(tmp_path):
    """Tier 2: action=retry / discard / prompt_required leave plan unchanged.

    Skip is the only policy that needs plan augmentation; others either
    re-execute (retry), abort (discard), or wait for user (prompt).
    """
    coord = SkillResumeCoordinator()
    plan = _plan_with_ambiguous()

    for policy_name, expected_action in [
        ("retry", "retry"),
        ("discard_skill", "discard"),
        ("prompt", "prompt_required"),
    ]:
        decision = coord.decide_for_plan(plan, SkillResumeConfig(default=policy_name))
        assert decision.action == expected_action
        # No injected CommittedStep for ambiguous (left raw for caller to handle)
        ids = [s.op_invocation_id for s in decision.plan.committed_steps]
        assert "draft.0" not in ids, (
            f"policy {policy_name} must NOT inject a committed step for the "
            f"ambiguous; got {decision.plan.committed_steps}"
        )


def test_skip_with_no_ambiguous_returns_plain_resume():
    """Tier 2: no ambiguous → action=resume (not skip), nothing to inject."""
    coord = SkillResumeCoordinator()
    plan = ResumePlan(
        run_id="run_clean",
        skill_name="demo",
        skill_input={},
        current_phase="draft",
        last_phase_artifact_path=None,
        awaiting_intervention_id=None,
        committed_steps=[],
        ambiguous_steps=[],
    )
    decision = coord.decide_for_plan(plan, SkillResumeConfig(default="skip"))
    assert decision.action == "resume"
    assert decision.plan.committed_steps == []


def test_skip_synthetic_step_memoizes_via_dispatch_tool(tmp_path, monkeypatch):
    """Tier 2: dispatch_tool memo lookup hits the skip-injected synthetic step.

    End-to-end check that the synthetic CommittedStep produced by skip is
    actually shaped correctly to satisfy ``_lookup_memoized_step``.
    """
    monkeypatch.chdir(tmp_path)

    from reyn.dispatch.dispatcher import (
        _lookup_memoized_step,
    )

    # Build the args_hash the dispatch_tool would compute for a real op
    op_args = {"op": "write", "path": "x.txt", "content": "x"}
    args_hash = _compute_args_hash(op_args)

    coord = SkillResumeCoordinator()
    plan = ResumePlan(
        run_id="run_skip2",
        skill_name="demo",
        skill_input={},
        current_phase="draft",
        last_phase_artifact_path=None,
        awaiting_intervention_id=None,
        committed_steps=[],
        ambiguous_steps=[
            AmbiguousStep(
                op_invocation_id="draft.0",
                op_kind="file",
                phase="draft",
                args_hash=args_hash,
                started_seq=10,
                args=op_args,
            ),
        ],
    )
    decision = coord.decide_for_plan(plan, SkillResumeConfig(default="skip"))

    # Memo lookup should hit
    memo = _lookup_memoized_step(
        decision.plan, "draft.0", "draft", args_hash,
    )
    assert memo is not None, (
        f"skip-synthesized CommittedStep must be findable by dispatch's "
        f"memo lookup; got plan.committed_steps={decision.plan.committed_steps}"
    )
