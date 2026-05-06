"""Tier 3a: copy_to_work phase validation judgment behavior (B6-S1-M1 仮説 a).

Hypothesis (a): when the preprocessor writes validation result to ``data.validation``
(without underscore prefix), the LLM reads it as a normal context field and bases
its routing decision on ``validation.ok``.

Two cases are pinned:
  - Case 1 (validation.ok=True):  LLM must transition to ``run_and_eval``.
  - Case 2 (validation.ok=False): LLM must abort (not continue).

Both fixtures include ``validation.ok`` and ``_resolved_paths`` in
``input_artifact.data`` — exactly as the preprocessor injects them in production.

The reason summary in each fixture explicitly references ``validation.ok``
(e.g. "validation.ok=true: ..."), demonstrating that the LLM read the field
and based its judgment on it, not on a default/blind transition.

Fixture source: hand-crafted (key computed from real ContextFrame serialization
matching the production call path; no real LLM call was made).
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from reyn.compiler.loader import load_dsl_skill
from reyn.llm.llm import call_llm
from reyn.schemas.models import (
    CandidateOutput,
    ContextFrame,
    ExecutionState,
    PhaseConstraints,
)
from reyn.testing.replay import REPLAY_DATETIME

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MODEL = "gemini-2.5-flash-lite"
SKILL_NAME = "skill_improver"
SKILL_DESC = (
    "Iteratively improve an existing skill by working on a temp copy, running eval, "
    "planning DSL changes, applying them, and re-evaluating until a score threshold is met. "
    "Only copies changes back to the original on success."
)
PHASE_ROLE = "workspace_initializer"

# Load from the worktree src tree so we pick up in-progress edits
_SKILL_PATH = (
    Path(__file__).parent.parent
    / "src"
    / "reyn"
    / "stdlib"
    / "skills"
    / "skill_improver"
    / "skill.md"
)


def _load_skill():
    return load_dsl_skill(_SKILL_PATH)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _candidate_run_and_eval(skill) -> CandidateOutput:
    """Build the run_and_eval candidate using the real loaded schema."""
    phase_run_eval = skill.phases["run_and_eval"]
    return CandidateOutput(
        next_phase="run_and_eval",
        control_type="transition",
        schema_name=phase_run_eval.input_schema_name,
        artifact_schema=phase_run_eval.input_schema,
        description="Transition to run_and_eval to start evaluation",
    )


def _make_frame(skill, validation_ok: bool) -> ContextFrame:
    """Build a ContextFrame as OSRuntime would after the preprocessor runs.

    ``input_artifact.data.validation`` is set to the preprocessor output,
    mirroring the production path after Step 8 (validate_copy) in
    copy_to_work.md.  ``_resolved_paths`` is set similarly (Step 9).
    """
    phase = skill.phases["copy_to_work"]
    return ContextFrame(
        current_phase="copy_to_work",
        current_phase_role=PHASE_ROLE,
        instructions=phase.instructions,
        candidate_outputs=[_candidate_run_and_eval(skill)],
        finish_criteria=[],
        constraints=PhaseConstraints(),
        available_control_ops=[],
        op_catalog=[],
        output_language="en",
        model="standard",
        model_resolved=MODEL,
        input_artifact={
            "type": "improvement_session",
            "data": {
                "target_skill": "direct_llm",
                "case_name": "basic",
                "case_input": "hello",
                "phase_criteria": [],
                "model": "standard",
                "max_iterations": 3,
                "score_threshold": 0.85,
                "improvement_focus": "",
                # validation written by preprocessor Step 8 (validate_copy)
                # with field name "data.validation" (no underscore prefix)
                "validation": {
                    "ok": validation_ok,
                    "files_written": 2 if validation_ok else 0,
                    "files_expected": 2,
                    "work_dir": ".reyn/skill_improver_work/direct_llm",
                },
                # resolved paths written by preprocessor Step 9 (inject_resolved_paths)
                "_resolved_paths": {
                    "target_skill_path": ".reyn/skill_improver_work/direct_llm/skill.md",
                    "target_dsl_root": ".reyn/skill_improver_work/direct_llm",
                    "eval_spec_path": "reyn/local/direct_llm/phases/eval.md",
                    "original_dsl_root": "reyn/local/direct_llm",
                },
            },
        },
        execution=ExecutionState(
            path=["prepare → copy_to_work"],
            current_visit=1,
            total_steps=1,
        ),
        control_ir_results=[],
        remaining_act_turns=0,  # decide-only phase (max_act_turns=0)
        current_datetime=REPLAY_DATETIME,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.replay("fixtures/llm/copy_to_work_validation/validation_ok.jsonl")
def test_copy_to_work_transitions_when_validation_ok():
    """Tier 3a (LLM replay): copy_to_work phase transitions to run_and_eval when validation.ok=True.

    Pins the LLM judgment behavior for the positive case: the preprocessor
    reports a successful copy (validation.ok=True, files_written=files_expected),
    and the LLM reads this from data.validation and transitions to run_and_eval.

    The fixture's reason summary explicitly references "validation.ok=true" —
    evidence that the LLM read the field rather than defaulting to continue.

    Hypothesis (a) verification: "data.validation" (no underscore) is accessible
    to the LLM as a normal context field.  If this test passes, the rename from
    "_validation" → "validation" (G2 fix, commit 3cf7412) made the field
    LLM-readable.
    """
    skill = _load_skill()
    frame = _make_frame(skill, validation_ok=True)

    result = asyncio.run(
        call_llm(
            MODEL,
            frame,
            prompt_cache_enabled=False,
            skill_name=SKILL_NAME,
            skill_description=SKILL_DESC,
            phase_role=PHASE_ROLE,
        )
    )

    data = result.data
    assert data["type"] == "decide", (
        f"Expected decide turn; got type={data.get('type')!r}"
    )
    ctrl = data["control"]

    # Key assertion: LLM correctly transitions on successful copy
    assert ctrl["type"] == "transition", (
        f"Expected transition when validation.ok=True; got control type={ctrl['type']!r}"
    )
    assert ctrl["next_phase"] == "run_and_eval", (
        f"Expected next_phase=run_and_eval; got {ctrl['next_phase']!r}"
    )
    assert ctrl["decision"] == "continue", (
        f"Expected decision=continue; got {ctrl['decision']!r}"
    )

    # The reason must reference validation — confirming the LLM read the field
    reason_summary = ctrl.get("reason", {}).get("summary", "")
    assert "validation" in reason_summary.lower(), (
        f"LLM reason does not reference 'validation' — may not have read the field. "
        f"summary={reason_summary!r}"
    )


@pytest.mark.replay("fixtures/llm/copy_to_work_validation/validation_fail.jsonl")
def test_copy_to_work_aborts_when_validation_fails():
    """Tier 3a (LLM replay): copy_to_work phase aborts when validation.ok=False.

    Pins the LLM judgment behavior for the failure case: the preprocessor
    reports a failed copy (validation.ok=False, files_written=0,
    files_expected=2), and the LLM reads this from data.validation and aborts.

    The fixture's reason summary explicitly references "validation.ok=false" —
    evidence that the LLM read the failure status rather than blindly continuing.

    If the LLM were ignoring data.validation (e.g. treating it as an internal
    field due to underscore naming convention or field position), it would
    instead transition to run_and_eval regardless of the copy result.
    This test distinguishes the two behaviors.

    Hypothesis (a) verification complement: validates that the LLM both reads
    the field AND acts correctly on failure — not just that it reads it on success.
    """
    skill = _load_skill()
    frame = _make_frame(skill, validation_ok=False)

    result = asyncio.run(
        call_llm(
            MODEL,
            frame,
            prompt_cache_enabled=False,
            skill_name=SKILL_NAME,
            skill_description=SKILL_DESC,
            phase_role=PHASE_ROLE,
        )
    )

    data = result.data
    assert data["type"] == "decide", (
        f"Expected decide turn; got type={data.get('type')!r}"
    )
    ctrl = data["control"]

    # Key assertion: LLM aborts on failed copy (does NOT blindly continue)
    assert ctrl["type"] == "abort", (
        f"Expected abort when validation.ok=False; got control type={ctrl['type']!r}. "
        f"If the LLM produced 'transition' here, it likely ignored data.validation — "
        f"hypothesis (a) might still apply (field was not read)."
    )
    assert ctrl["decision"] == "abort", (
        f"Expected decision=abort; got {ctrl['decision']!r}"
    )

    # The reason must reference the validation failure
    reason_summary = ctrl.get("reason", {}).get("summary", "")
    assert "validation" in reason_summary.lower(), (
        f"LLM reason does not reference 'validation' — may not have read the field. "
        f"summary={reason_summary!r}"
    )
