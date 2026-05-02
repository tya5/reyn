"""Replay tests for eval_builder per-case criteria generation.

Verifies that the ``analyze_skill`` phase produces:
1. A valid ``eval_analysis`` artifact with ``cases`` and ``criteria`` fields,
   where each case has its own criteria list (not a global flat list).
2. Drift detection: a modified input raises MissingFixture.

Fixtures are pre-recorded at ``tests/fixtures/llm/eval_builder/``.

Tier 3a: one typical case + one drift detection.
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
    """Tier 3a: analyze_skill produces cases with per-case criteria for article_generator."""
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

    for case in cases:
        assert "id" in case or "name" in case, f"Case missing id/name: {case}"
        assert "criteria" in case, f"Case missing per-case criteria: {case}"
        assert len(case["criteria"]) >= 1, f"Case has empty criteria list: {case}"
        for criterion in case["criteria"]:
            assert isinstance(criterion, str), f"Criterion should be a string: {criterion!r}"
            assert len(criterion) > 5, f"Criterion too short: {criterion!r}"


# ── test: missing fixture raises MissingFixture loudly ────────────────────────


@pytest.mark.replay("fixtures/llm/eval_builder/analyze_skill.jsonl")
def test_wrong_input_raises_missing_fixture():
    """Tier 3a drift detection: a modified input produces a different key → MissingFixture.

    Protects: if instructions or input artifact change (e.g. skill_path is
    different), the fixture key will not match. This ensures prompt drift is
    detected immediately rather than silently using a stale fixture.
    """
    from reyn.testing.replay import MissingFixture

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
