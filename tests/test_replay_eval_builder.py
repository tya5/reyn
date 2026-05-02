"""Replay tests for eval_builder per-case criteria generation.

Verifies that the ``analyze_skill`` phase produces:
1. A valid ``eval_analysis`` artifact with ``cases`` and ``criteria`` fields.
2. Per-case criteria — each case has its own list (not a global flat list).
3. A more complex analysis when the target skill has review/rollback loops.

Fixtures are pre-recorded at ``tests/fixtures/llm/eval_builder/``.
"""
from __future__ import annotations

import asyncio

import pytest

from reyn.llm.llm import call_llm
from reyn.schemas.models import (
    CandidateOutput,
    ContextFrame,
    ControlIROpSpec,
    ExecutionState,
    PhaseConstraints,
)
from reyn.testing.replay import REPLAY_DATETIME

MODEL = "gemini-2.5-flash-lite"
SKILL_NAME = "eval_builder"
SKILL_DESC = "Auto-generate an eval spec (eval.md) for a skill"


def _run(coro):
    return asyncio.run(coro)


def _candidate_write_eval() -> CandidateOutput:
    return CandidateOutput(
        next_phase="write_eval",
        control_type="transition",
        schema_name="eval_analysis",
        artifact_schema={
            "type": "object",
            "properties": {
                "skill_path": {"type": "string"},
                "cases": {"type": "array", "items": {}},
                "summary": {"type": "string"},
            },
            "required": ["skill_path", "cases", "summary"],
        },
        description="Transition to write_eval with analyzed cases",
    )


def _op_file() -> ControlIROpSpec:
    return ControlIROpSpec(
        kind="file",
        description="Read a file",
        example={"kind": "file", "op": "read", "path": "dsl/skills/article_generator/skill.md"},
    )


# ── test: analyze_skill — basic article_generator ────────────────────────────


@pytest.mark.replay("fixtures/llm/eval_builder/analyze_skill.jsonl")
def test_analyze_skill_produces_per_case_criteria():
    """analyze_skill produces cases with per-case criteria for article_generator."""
    frame = ContextFrame(
        current_phase="analyze_skill",
        current_phase_role="eval_builder",
        instructions="Analyze the skill and design representative test cases with per-case criteria.",
        candidate_outputs=[_candidate_write_eval()],
        finish_criteria=["cases designed"],
        constraints=PhaseConstraints(),
        available_control_ops=[_op_file()],
        op_catalog=[],
        output_language="en",
        model="openai/gemini-2.5-flash-lite",
        model_resolved=MODEL,
        input_artifact={
            "type": "user_message",
            "data": {
                "text": "Generate an eval spec for the article_generator skill.",
                "skill_path": "dsl/skills/article_generator",
            },
        },
        execution=ExecutionState(path=[], current_visit=1, total_steps=0),
        control_ir_results=[],
        remaining_act_turns=2,
        current_datetime=REPLAY_DATETIME,
    )

    result = _run(
        call_llm(
            MODEL,
            frame,
            prompt_cache_enabled=False,
            skill_name=SKILL_NAME,
            skill_description=SKILL_DESC,
            phase_role="eval_builder",
        )
    )

    data = result.data
    assert data["type"] == "decide"
    ctrl = data["control"]
    assert ctrl["type"] == "transition"
    assert ctrl["next_phase"] == "write_eval"

    artifact = data["artifact"]
    assert artifact["type"] == "eval_analysis"
    analysis = artifact["data"]

    assert "skill_path" in analysis
    assert "cases" in analysis
    assert "summary" in analysis

    cases = analysis["cases"]
    assert len(cases) >= 1, "Expected at least one test case"

    # Each case should have per-case criteria
    for case in cases:
        assert "id" in case or "name" in case, f"Case missing id/name: {case}"
        assert "criteria" in case, f"Case missing per-case criteria: {case}"
        assert len(case["criteria"]) >= 1, f"Case has empty criteria list: {case}"
        for criterion in case["criteria"]:
            assert isinstance(criterion, str), f"Criterion should be a string: {criterion!r}"
            assert len(criterion) > 5, f"Criterion too short: {criterion!r}"


# ── test: analyze_skill — skill with rollback loop ────────────────────────────


@pytest.mark.replay("fixtures/llm/eval_builder/analyze_with_rollback.jsonl")
def test_analyze_skill_with_rollback_includes_revision_case():
    """analyze_skill for a skill with a review/rollback loop includes a revision case."""
    frame = ContextFrame(
        current_phase="analyze_skill",
        current_phase_role="eval_builder",
        instructions="Analyze the skill and design representative test cases with per-case criteria.",
        candidate_outputs=[_candidate_write_eval()],
        finish_criteria=["cases designed"],
        constraints=PhaseConstraints(),
        available_control_ops=[_op_file()],
        op_catalog=[],
        output_language="en",
        model="openai/gemini-2.5-flash-lite",
        model_resolved=MODEL,
        input_artifact={
            "type": "user_message",
            "data": {
                "text": "Generate eval spec for the writing_review_app skill (has a review/rollback loop).",
                "skill_path": "dsl/skills/writing_review_app",
            },
        },
        execution=ExecutionState(path=[], current_visit=1, total_steps=0),
        control_ir_results=[],
        remaining_act_turns=2,
        current_datetime=REPLAY_DATETIME,
    )

    result = _run(
        call_llm(
            MODEL,
            frame,
            prompt_cache_enabled=False,
            skill_name=SKILL_NAME,
            skill_description=SKILL_DESC,
            phase_role="eval_builder",
        )
    )

    data = result.data
    assert data["type"] == "decide"

    artifact = data["artifact"]
    analysis = artifact["data"]

    cases = analysis["cases"]
    # Should include at least 2 cases: happy path + revision/rollback
    assert len(cases) >= 2, (
        f"Expected >= 2 cases for a skill with rollback; got {len(cases)}"
    )

    # All cases should have per-case criteria
    total_criteria = sum(len(c.get("criteria", [])) for c in cases)
    assert total_criteria >= 4, (
        f"Expected >= 4 total criteria across cases; got {total_criteria}"
    )

    # At least one case should describe a rollback/revision scenario
    all_text = " ".join(
        " ".join(c.get("criteria", [])) + " " + c.get("name", "")
        for c in cases
    ).lower()
    rollback_keywords = ["rollback", "revision", "review", "reject", "revise"]
    assert any(kw in all_text for kw in rollback_keywords), (
        f"Expected a rollback/revision case in the analysis; got: {all_text[:300]}"
    )


# ── test: missing fixture raises MissingFixture loudly ────────────────────────


@pytest.mark.replay("fixtures/llm/eval_builder/analyze_skill.jsonl")
def test_wrong_input_raises_missing_fixture():
    """A modified input produces a different key → MissingFixture is raised."""
    from reyn.testing.replay import MissingFixture

    # Same phase but DIFFERENT skill_path — different key
    frame = ContextFrame(
        current_phase="analyze_skill",
        current_phase_role="eval_builder",
        instructions="Analyze the skill and design representative test cases with per-case criteria.",
        candidate_outputs=[_candidate_write_eval()],
        finish_criteria=["cases designed"],
        constraints=PhaseConstraints(),
        available_control_ops=[_op_file()],
        op_catalog=[],
        output_language="en",
        model="openai/gemini-2.5-flash-lite",
        model_resolved=MODEL,
        input_artifact={
            "type": "user_message",
            "data": {
                "text": "Generate an eval spec for the article_generator skill.",
                "skill_path": "dsl/skills/DIFFERENT_SKILL",  # <-- modified
            },
        },
        execution=ExecutionState(path=[], current_visit=1, total_steps=0),
        control_ir_results=[],
        remaining_act_turns=2,
        current_datetime=REPLAY_DATETIME,
    )

    with pytest.raises(MissingFixture, match="No fixture entry"):
        _run(
            call_llm(
                MODEL,
                frame,
                prompt_cache_enabled=False,
                skill_name=SKILL_NAME,
                skill_description=SKILL_DESC,
                phase_role="eval_builder",
            )
        )
