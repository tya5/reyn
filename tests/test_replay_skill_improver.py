"""Replay tests for skill_improver temp-copy workflow.

Verifies that:
1. The ``prepare`` phase produces a valid ``work_config`` artifact with skill
   path, work path, score threshold, and max iterations.
2. The ``force_decide`` path (``remaining_act_turns=0``) still produces a valid
   decide turn — the LLM is not allowed to emit act ops.

Both scenarios use pre-recorded fixtures so the tests are fully deterministic.

Note: drift detection test for this area is deferred to a future PR (no new
tests may be added in PR28 step 2). Coverage checklist item is tracked in
the PR28 plan.

Tier 3a: two cases (typical + force_decide boundary).
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
    """Tier 3a: prepare phase transitions to copy_to_work with a valid work_config."""
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

    assert 0.0 < cfg["score_threshold"] <= 1.0
    assert cfg["max_iterations"] >= 1
    assert isinstance(cfg["skill_path"], str)
    assert isinstance(cfg["work_path"], str)


# ── test: force_decide path — remaining_act_turns=0 ──────────────────────────


@pytest.mark.replay("fixtures/llm/skill_improver/force_decide.jsonl")
def test_force_decide_produces_decide_turn():
    """Tier 3a: when remaining_act_turns=0, LLM must emit a decide turn (force_decide path)."""
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
    assert data["type"] == "decide", (
        "force_decide path: LLM must emit a decide turn when remaining_act_turns=0"
    )
    ctrl = data["control"]
    assert ctrl["type"] == "transition"
    assert ctrl["next_phase"] == "copy_to_work"

    cfg = data["artifact"]["data"]
    assert "skill_path" in cfg
    assert "work_path" in cfg
    assert "score_threshold" in cfg
    assert "max_iterations" in cfg


# ── corner case: validation fails after attempt → force_decide engaged ────────


def _candidate_apply_finalize() -> CandidateOutput:
    return CandidateOutput(
        next_phase="finalize",
        control_type="transition",
        schema_name="improvement_result",
        artifact_schema={
            "type": "object",
            "properties": {
                "target_skill_path": {"type": "string"},
                "iterations_performed": {"type": "integer"},
                "initial_score": {"type": "number"},
                "final_score": {"type": "number"},
                "termination_reason": {"type": "string"},
                "summary": {"type": "string"},
                "score_history": {"type": "array", "items": {"type": "number"}},
                "files_modified": {"type": "array", "items": {"type": "string"}},
                "work_dsl_root": {"type": "string"},
                "original_dsl_root": {"type": "string"},
                "copied_back": {"type": "boolean"},
                "next_steps": {"type": "string"},
            },
            "required": [
                "target_skill_path",
                "iterations_performed",
                "final_score",
                "termination_reason",
                "summary",
            ],
        },
        description="Hand off to finalize with the improvement result",
    )


@pytest.mark.replay("fixtures/llm/skill_improver/validation_fails_after_attempt.jsonl")
def test_validation_fails_after_attempt_force_decides():
    """Tier 3a corner: prior_attempts shows validation failure → LLM must produce a valid artifact this time.

    Protects: the prior_attempts injection path. When a phase has previously
    emitted an artifact that failed validation, the next call shows that
    history. The LLM must not repeat the same mistake — under
    remaining_act_turns=0 (force_decide), it must emit a *valid* decide turn.
    """
    frame = ContextFrame(
        current_phase="prepare",
        current_phase_role="skill_improver",
        instructions="Analyze the skill improvement request and prepare a work configuration.",
        candidate_outputs=[_candidate_copy_to_work()],
        finish_criteria=["work_config ready"],
        constraints=PhaseConstraints(),
        available_control_ops=[],
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
        remaining_act_turns=0,
        current_datetime=REPLAY_DATETIME,
    )

    prior_attempts = [
        {
            "raw": '{"type": "decide", "control": {"type": "transition", "next_phase": "copy_to_work", "decision": "continue"}, "artifact": {"type": "work_config", "data": {"skill_path": "dsl/skills/article_generator"}}}',
            "error": "Artifact validation failed: missing required fields ['work_path', 'score_threshold', 'max_iterations']",
        }
    ]

    result = _run(
        call_llm(
            MODEL,
            frame,
            prior_attempts=prior_attempts,
            prompt_cache_enabled=False,
            skill_name=SKILL_NAME,
            skill_description=SKILL_DESC,
            phase_role="skill_improver",
        )
    )

    data = result.data
    assert data["type"] == "decide", "force_decide: must produce a decide turn"
    ctrl = data["control"]
    assert ctrl["type"] == "transition"
    assert ctrl["next_phase"] == "copy_to_work"

    # On the corrected attempt all required fields must now be present.
    cfg = data["artifact"]["data"]
    for field in ("skill_path", "work_path", "score_threshold", "max_iterations"):
        assert field in cfg, (
            f"After validation failure, retry still missing required field: {field}"
        )


# ── corner case: improvement makes the score worse → rollback or regression_detected ─────


def _candidate_loop_back() -> CandidateOutput:
    return CandidateOutput(
        next_phase="run_and_eval",
        control_type="transition",
        schema_name="rollback",
        artifact_schema={
            "type": "object",
            "properties": {},
        },
        description="Roll back to run_and_eval to start the next iteration",
    )


@pytest.mark.xfail(
    reason=(
        "DOGFOOD BUG (high): apply_improvements rolls back to run_and_eval even "
        "when the LLM's reason field correctly identifies a regression "
        "(0.72 → 0.55). The recorded fixture shows reason.summary='Regression "
        "detected: ... Rolling back ...' but next_phase='run_and_eval' — the "
        "termination logic in skill.md step 3 is not being honoured. "
        "Remove this xfail when skill.md / phase prompt is fixed."
    ),
    strict=True,
)
@pytest.mark.replay("fixtures/llm/skill_improver/improvement_makes_worse.jsonl")
def test_improvement_regression_handled():
    """Tier 3a corner: latest_eval < previous score → finalize with regression_detected.

    Per skill.md (apply_improvements step 3), regression_detected is the
    termination reason when latest_eval.overall_score < history[-1].eval_score
    after iteration > 1. The LLM must transition to finalize and not loop.
    """
    frame = ContextFrame(
        current_phase="apply_improvements",
        current_phase_role="implementer",
        instructions=(
            "After applying changes, decide whether to finalize or loop. "
            "Detect regression when latest_eval.overall_score < history[-1].eval_score "
            "and current_iteration > 1; in that case transition to finalize with "
            "termination_reason='regression_detected'."
        ),
        candidate_outputs=[_candidate_apply_finalize(), _candidate_loop_back()],
        finish_criteria=["iteration committed", "decided to finalize or loop"],
        constraints=PhaseConstraints(),
        available_control_ops=[],
        op_catalog=[],
        output_language="en",
        model="openai/gemini-2.5-flash-lite",
        model_resolved=MODEL,
        input_artifact={
            "type": "improvement_plan",
            "data": {
                "summary": "Tightened the prompt for the generate_article phase.",
                "changes": [],  # changes already applied earlier; no further ops
                "iteration_state": {
                    "current_iteration": 2,
                    "latest_eval": {
                        "overall_score": 0.55,
                        "weakest_phase": "generate_article",
                    },
                    "session": {
                        "target_skill_path": "dsl/skills/article_generator",
                        "target_dsl_root": ".reyn/skill_improver_work/article_generator/",
                        "original_dsl_root": "dsl/skills/article_generator/",
                        "score_threshold": 0.85,
                        "max_iterations": 3,
                    },
                    "history": [
                        {
                            "iteration": 1,
                            "eval_score": 0.72,
                            "weakest_phase": "generate_article",
                            "files_modified": [],
                            "plan_summary": "initial baseline",
                        }
                    ],
                },
            },
        },
        execution=ExecutionState(
            path=["prepare", "copy_to_work", "run_and_eval", "plan_improvements", "apply_improvements"],
            current_visit=2,
            total_steps=5,
        ),
        control_ir_results=[
            {
                "kind": "file",
                "op": "read",
                "path": ".reyn/improver_state.json",
                "content": '{"session": {"target_skill_path": "dsl/skills/article_generator"}, "iterations": [{"iteration": 1, "eval_score": 0.72, "weakest_phase": "generate_article", "files_modified": [], "plan_summary": "initial baseline"}]}',
                "status": "ok",
            },
            {
                "kind": "file",
                "op": "write",
                "path": ".reyn/improver_state.json",
                "status": "ok",
            },
        ],
        remaining_act_turns=0,  # decide turn
        current_datetime=REPLAY_DATETIME,
    )

    result = _run(
        call_llm(
            MODEL,
            frame,
            prompt_cache_enabled=False,
            skill_name=SKILL_NAME,
            skill_description=SKILL_DESC,
            phase_role="implementer",
        )
    )

    data = result.data
    assert data["type"] == "decide"
    ctrl = data["control"]
    # Regression: must terminate, not loop. Either finalize with regression_detected,
    # or any termination — but NOT a fresh rollback to run_and_eval.
    assert ctrl["type"] == "transition", (
        f"Expected transition to finalize on regression, got control type: {ctrl['type']!r}"
    )
    assert ctrl["next_phase"] == "finalize", (
        f"Regression should hand off to finalize, not loop back: got {ctrl['next_phase']!r}"
    )
    artifact = data["artifact"]
    assert artifact["type"] == "improvement_result"
    result_data = artifact["data"]
    # The termination_reason for a strictly worse iteration should be regression_detected.
    assert result_data.get("termination_reason") == "regression_detected", (
        f"Expected termination_reason='regression_detected' for score drop, "
        f"got {result_data.get('termination_reason')!r}"
    )
