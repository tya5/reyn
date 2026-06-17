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

from reyn.dev.testing.replay import REPLAY_DATETIME
from reyn.llm.llm import call_llm
from reyn.schemas.models import (
    CandidateOutput,
    ContextFrame,
    ControlIROpSpec,
    ExecutionState,
    PhaseConstraints,
)

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
    # #1240 Wave 2b: kept as the COARSE "file" spec intentionally. This is test
    # scaffolding for the LLM-replay frame, NOT the migrated skill's real
    # advertised catalog (which is now the 6 fine kinds). Safe because: (i) these
    # are decide-turn tests whose assertions are catalog-INSENSITIVE (the analysis
    # output doesn't depend on which file ops are advertised); (ii) the act-path
    # (fine-op emission → executor route) fidelity is covered by the Batch-1
    # dogfood (judge_phase, same available_ops mechanism); (iii) the coarse
    # FileIROp(kind="file") model is KEPT in the ControlIROp union as the shared
    # execution backend, so this spec still validates after the coarse kind was
    # dropped from OP_KIND_MODEL_MAP / the LLM catalog.
    #
    # ★ A faithful fine-op re-record was attempted and DEFERRED out of Wave 2b:
    # advertising the fine kinds surfaces a *legitimate behavior change* —
    # analyze_skill is a read-heavy phase, so under fine ops the LLM emits a
    # file-read ACT turn before deciding (correct, phase-as-designed), whereas the
    # recorded coarse fixture goes straight to a decide turn that designs cases
    # without reading. Faithfully testing the read-heavy phase under fine ops
    # therefore needs the single-call replay restructured to represent post-read
    # state (file contents fed via control_ir_results) — a test-fidelity change,
    # not a mechanical re-record. Tracked as a follow-up under #1240.
    return ControlIROpSpec(
        kind="file",
        description="Read a file",
        example={"kind": "file", "op": "read", "path": "reyn/project/article_generator/skill.md"},
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
                "skill_path": "reyn/project/article_generator",
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


# ── test: missing fixture raises MissingFixture loudly ────────────────────────


@pytest.mark.replay("fixtures/llm/eval_builder/analyze_skill.jsonl")
def test_wrong_input_raises_missing_fixture():
    """Tier 3a: drift detection: a modified input produces a different key → MissingFixture.

    Protects: if instructions or input artifact change (e.g. skill_path is
    different), the fixture key will not match. This ensures prompt drift is
    detected immediately rather than silently using a stale fixture.
    """
    from reyn.dev.testing.replay import MissingFixture

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
                "skill_path": "reyn/project/DIFFERENT_SKILL",  # <-- modified
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


# ── corner case: malformed prior attempt (non-JSON) — recovery path ──────────


@pytest.mark.replay("fixtures/llm/eval_builder/malformed_criteria.jsonl")
def test_analyze_skill_recovers_from_malformed_prior_attempt():
    """Tier 3a: corner: prior attempt produced non-JSON criteria → next call must produce valid JSON.

    Protects: when a previous LLM attempt emitted non-parseable output (a
    common failure mode), the prior_attempts injection plus the OS retry
    mechanism must drive the next call to produce a structurally valid
    artifact. We pin: cases is present, criteria are non-empty strings.
    """
    frame = ContextFrame(
        current_phase="analyze_skill",
        current_phase_role="eval_builder",
        instructions="Analyze the skill and design representative test cases with per-case criteria.",
        candidate_outputs=[_candidate_write_eval()],
        finish_criteria=["cases designed"],
        constraints=PhaseConstraints(),
        available_control_ops=[],  # force decide
        op_catalog=[],
        output_language="en",
        model="openai/gemini-2.5-flash-lite",
        model_resolved=MODEL,
        input_artifact={
            "type": "user_message",
            "data": {
                "text": "Generate an eval spec for the article_generator skill.",
                "skill_path": "reyn/project/article_generator",
            },
        },
        execution=ExecutionState(
            path=["analyze_skill"], current_visit=1, total_steps=1,
        ),
        control_ir_results=[
            {
                "kind": "file",
                "op": "read",
                "path": "reyn/project/article_generator/skill.md",
                "content": "# article_generator\n\nGenerates a polished article on a given topic.",
                "status": "ok",
            }
        ],
        remaining_act_turns=0,  # force decide after a bad attempt
        current_datetime=REPLAY_DATETIME,
    )

    prior_attempts = [
        {
            "raw": "I will produce 3 test cases for the article generator: a happy path, an empty input, and a multilingual input.",
            "error": "Output is not valid JSON; expected an object with type/control/artifact keys.",
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
            phase_role="eval_builder",
        )
    )

    data = result.data
    assert data["type"] == "decide", "After a non-JSON attempt, recovery must produce a decide turn"
    artifact = data["artifact"]
    assert artifact["type"] == "eval_analysis"
    cases = artifact["data"].get("cases", [])
    assert len(cases) >= 1
    for case in cases:
        criteria = case.get("criteria", [])
        assert len(criteria) >= 1
        for c in criteria:
            assert isinstance(c, str) and len(c) > 0


# ── corner case: conflicting per-case criteria — what does the skill produce? ─


@pytest.mark.replay("fixtures/llm/eval_builder/conflicting_per_case_criteria.jsonl")
def test_analyze_skill_with_conflicting_user_criteria():
    """Tier 3a: corner: user states two requirements that cannot both hold for one phase.

    The user's brief embeds a logical contradiction: 'always reject any input
    in any language other than English' AND 'always accept Japanese input
    gracefully'. This is the kind of input that surfaces in real dogfooding
    when a user pastes inconsistent specs. The skill should still produce a
    structurally valid eval_analysis (cases with criteria) — we don't pin
    which side of the contradiction it picks. The job of catching the
    contradiction belongs to the eval skill (LLM-as-judge), not the test.
    """
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
                "text": (
                    "Generate an eval spec for the article_generator skill. "
                    "REQUIREMENT A: the skill must always reject any input in any "
                    "language other than English with a clear error message. "
                    "REQUIREMENT B: the skill must always gracefully produce a "
                    "Japanese article when the input topic is in Japanese. "
                    "Make sure both requirements are reflected in the criteria."
                ),
                "skill_path": "reyn/project/article_generator",
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

    # The skill may emit a transition (analysis done) OR an act turn that reads
    # additional files (the natural first move). Either is structurally valid.
    if data["type"] == "act":
        ops = data.get("ops", [])
        assert isinstance(ops, list) and len(ops) >= 1, (
            "act turn must include at least one op"
        )
        # The skill is reading files; cannot assert on cases yet.
        return

    assert data["type"] == "decide"
    if data["control"]["type"] == "transition":
        artifact = data["artifact"]
        assert artifact["type"] == "eval_analysis"
        cases = artifact["data"].get("cases", [])
        assert len(cases) >= 1
        for case in cases:
            assert "criteria" in case
            assert len(case["criteria"]) >= 1
