"""Replay tests for skill_improver temp-copy workflow.

Verifies that:
1. The ``prepare`` phase produces a valid ``work_config`` artifact with skill
   path, work path, score threshold, and max iterations.
2. The ``force_decide`` path (``remaining_act_turns=0``) still produces a valid
   decide turn — the LLM is not allowed to emit act ops.

Both scenarios use pre-recorded fixtures so the tests are fully deterministic.
"""
from __future__ import annotations

import asyncio

import pytest

from reyn.llm import call_llm
from reyn.models import (
    CandidateOutput,
    ContextFrame,
    ControlIROpSpec,
    ExecutionState,
    PhaseConstraints,
)
from reyn.testing.replay import REPLAY_DATETIME

MODEL = "gemini-2.5-flash-lite"
SKILL_NAME = "skill_improver"
SKILL_DESC = (
    "Iteratively improve an existing skill by working on a temp copy, running eval, "
    "planning DSL changes, applying them, and re-evaluating until a score threshold is met."
)


def _run(coro):
    return asyncio.run(coro)


def _candidate_copy_to_work() -> CandidateOutput:
    return CandidateOutput(
        next_phase="copy_to_work",
        control_type="transition",
        schema_name="work_config",
        artifact_schema={
            "type": "object",
            "properties": {
                "skill_path": {"type": "string"},
                "work_path": {"type": "string"},
                "score_threshold": {"type": "number"},
                "max_iterations": {"type": "integer"},
            },
            "required": ["skill_path", "work_path", "score_threshold", "max_iterations"],
        },
        description="Transition to copy_to_work with configuration",
    )


def _op_file() -> ControlIROpSpec:
    return ControlIROpSpec(
        kind="file",
        description="Read a file",
        example={"kind": "file", "op": "read", "path": "dsl/skills/article_generator/skill.md"},
    )


# ── test: prepare phase produces work_config ──────────────────────────────────


@pytest.mark.replay("fixtures/llm/skill_improver/prepare_phase.jsonl")
def test_prepare_phase_produces_work_config():
    """prepare phase transitions to copy_to_work with a valid work_config."""
    frame = ContextFrame(
        current_phase="prepare",
        current_phase_role="skill_improver",
        instructions="Analyze the skill improvement request and prepare a work configuration.",
        candidate_outputs=[_candidate_copy_to_work()],
        finish_criteria=["work_config ready"],
        constraints=PhaseConstraints(),
        available_control_ops=[_op_file()],
        op_catalog=[],
        output_language="en",
        model="openai/gemini-2.5-flash-lite",
        model_resolved=MODEL,
        input_artifact={
            "type": "user_message",
            "data": {
                "text": "Please improve the article_generator skill to get above 0.8 eval score.",
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
            phase_role="skill_improver",
        )
    )

    data = result.data
    assert data["type"] == "decide"
    ctrl = data["control"]
    assert ctrl["type"] == "transition"
    assert ctrl["next_phase"] == "copy_to_work"
    assert ctrl["decision"] == "continue"

    artifact = data["artifact"]
    assert artifact["type"] == "work_config"
    cfg = artifact["data"]
    assert "skill_path" in cfg
    assert "work_path" in cfg
    assert "score_threshold" in cfg
    assert "max_iterations" in cfg

    # Threshold should be reasonable (0.0–1.0)
    assert 0.0 < cfg["score_threshold"] <= 1.0
    assert cfg["max_iterations"] >= 1

    # The skill_path and work_path should be strings
    assert isinstance(cfg["skill_path"], str)
    assert isinstance(cfg["work_path"], str)


# ── test: force_decide path — remaining_act_turns=0 ──────────────────────────


@pytest.mark.replay("fixtures/llm/skill_improver/force_decide.jsonl")
def test_force_decide_produces_decide_turn():
    """When remaining_act_turns=0, LLM must emit a decide turn (force_decide path)."""
    frame = ContextFrame(
        current_phase="prepare",
        current_phase_role="skill_improver",
        instructions="Analyze the skill improvement request and prepare a work configuration.",
        candidate_outputs=[_candidate_copy_to_work()],
        finish_criteria=["work_config ready"],
        constraints=PhaseConstraints(),
        available_control_ops=[],  # no ops available — force decide
        op_catalog=[],
        output_language="en",
        model="openai/gemini-2.5-flash-lite",
        model_resolved=MODEL,
        input_artifact={
            "type": "user_message",
            "data": {
                "text": "Improve the article_generator skill.",
                "skill_path": "dsl/skills/article_generator",
            },
        },
        execution=ExecutionState(path=[], current_visit=1, total_steps=1),
        control_ir_results=[
            {
                "kind": "file",
                "op": "read",
                "path": "dsl/skills/article_generator/skill.md",
                "content": "# article_generator skill",
                "status": "ok",
            }
        ],
        remaining_act_turns=0,  # force_decide
        current_datetime=REPLAY_DATETIME,
    )

    result = _run(
        call_llm(
            MODEL,
            frame,
            prompt_cache_enabled=False,
            skill_name=SKILL_NAME,
            skill_description=SKILL_DESC,
            phase_role="skill_improver",
        )
    )

    data = result.data
    # Must be a decide turn, not an act turn — remaining_act_turns=0 forbids act
    assert data["type"] == "decide", (
        "force_decide path: LLM must emit a decide turn when remaining_act_turns=0"
    )
    ctrl = data["control"]
    # Should still transition to copy_to_work with whatever info it has
    assert ctrl["type"] == "transition"
    assert ctrl["next_phase"] == "copy_to_work"

    artifact = data["artifact"]
    cfg = artifact["data"]
    # Even under force_decide, work_config fields must be populated
    assert "skill_path" in cfg
    assert "work_path" in cfg
    assert "score_threshold" in cfg
    assert "max_iterations" in cfg
