"""Tier 2: judge_phase postprocessor output_schema wrapping contract.

Pins the fix for B37 W3 S8: the judge_phase skill's postprocessor
output_schema must be the full {type, data} envelope schema so that
PostprocessorExecutor.run()'s final jsonschema validation succeeds
against the wrapped result artifact.

Root cause: the inline dict literal output_schema in skill.md bypassed
artifact_to_json_schema() wrapping. Switching to a string artifact name
reference ("phase_judgment") makes the compiler wrap it correctly.

Two contract tests:

  test_judge_phase_postprocessor_output_schema_is_wrapped
    - Loads the real judge_phase skill from the stdlib directory.
    - Asserts postprocessor.output_schema has the {type, data} envelope
      shape required by PostprocessorExecutor.run().
    - Pins the structural fix: schema must have "type" and "data" as
      required top-level properties.

  test_judge_phase_postprocessor_schema_accepts_valid_result
    - Validates a minimal fully-valid postprocessor output against the
      loaded output_schema — must produce zero jsonschema errors.

Both tests use real instances (no mocks). Tier 2 (OS / compiler contract).
"""
from __future__ import annotations

import json

# ---------------------------------------------------------------------------
# Load the real judge_phase skill from the stdlib directory.
# ---------------------------------------------------------------------------
import sys
from pathlib import Path

import jsonschema
import pytest

# Ensure the package is importable when running from repo root.
_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from reyn.compiler.loader import load_dsl_skill

_SKILL_PATH = (
    Path(__file__).resolve().parent.parent
    / "src" / "reyn" / "stdlib" / "skills" / "judge_phase" / "skill.md"
)


@pytest.fixture(scope="module")
def judge_skill():
    """Load judge_phase once per module (read-only fixture)."""
    return load_dsl_skill(_SKILL_PATH)


# ---------------------------------------------------------------------------
# Test 1: output_schema is the wrapped {type, data} envelope
# ---------------------------------------------------------------------------


def test_judge_phase_postprocessor_output_schema_is_wrapped(judge_skill) -> None:
    """Tier 2: judge_phase postprocessor.output_schema has {type, data} envelope.

    B37 W3 S8 root cause: the inline dict output_schema was a plain data
    schema (fields at top level). PostprocessorExecutor.run() validates the
    full wrapped result {type, data} against it — so every data field appeared
    as a missing required property at the top level.

    After the fix (output_schema: phase_judgment string reference), the
    compiler wraps it via artifact_to_json_schema(), producing:
      {type: object, properties: {type: {const: phase_judgment}, data: {...}},
       required: [type, data]}
    """
    pp = judge_skill.postprocessor
    assert pp is not None, "judge_phase must have a postprocessor block"

    schema = pp.output_schema

    # Top-level must be the {type, data} envelope.
    assert schema.get("type") == "object", (
        f"output_schema top-level type must be 'object'; got {schema.get('type')!r}"
    )
    assert "type" in schema.get("required", []), (
        "output_schema must require 'type' at the top level (envelope fix)"
    )
    assert "data" in schema.get("required", []), (
        "output_schema must require 'data' at the top level (envelope fix)"
    )

    # The 'type' property must constrain to 'phase_judgment'.
    type_prop = schema.get("properties", {}).get("type", {})
    assert type_prop.get("const") == "phase_judgment", (
        f"output_schema.properties.type.const must be 'phase_judgment'; "
        f"got {type_prop.get('const')!r}"
    )

    # The 'data' sub-schema must require the core judgment fields.
    data_schema = schema.get("properties", {}).get("data", {})
    data_required = set(data_schema.get("required", []))
    expected_required = {"phase_name", "passed", "score", "criteria_results", "summary"}
    missing = expected_required - data_required
    assert not missing, (
        f"data sub-schema missing required fields: {sorted(missing)}"
    )


# ---------------------------------------------------------------------------
# Test 2: a fully-valid postprocessor result passes schema validation
# ---------------------------------------------------------------------------


def test_judge_phase_postprocessor_schema_accepts_valid_result(judge_skill) -> None:
    """Tier 2: fully-valid postprocessor result validates against output_schema.

    Constructs a minimal complete {type, data} result — as PostprocessorExecutor
    would produce after compute_score injects data.score — and confirms that
    jsonschema.Draft7Validator produces zero errors.

    This directly reproduces the B37 W3 S8 failure mode: before the fix,
    every data field (phase_name, passed, score, criteria_results, summary)
    appeared as a missing required property because the schema was unwrapped.
    """
    pp = judge_skill.postprocessor
    assert pp is not None

    # Minimal valid result: exactly what PostprocessorExecutor returns after
    # compute_score runs and the artifact type is renamed to 'phase_judgment'.
    valid_result = {
        "type": "phase_judgment",
        "data": {
            "phase_name": "judge",
            "passed": True,
            "score": 1.0,
            "criteria_results": [
                {
                    "description": "Output addresses all criteria",
                    "met": True,
                    "reason": "The artifact satisfies the criterion.",
                }
            ],
            "summary": "All required criteria are met.",
        },
    }

    validator = jsonschema.Draft7Validator(pp.output_schema)
    errors = sorted(validator.iter_errors(valid_result), key=str)
    assert errors == [], (
        f"Valid postprocessor result must pass output_schema validation; "
        f"got {len(errors)} errors:\n"
        + "\n".join(f"  - {e.message}" for e in errors)
    )


# ---------------------------------------------------------------------------
# Test 3: result with only score in data (missing other fields) fails
# ---------------------------------------------------------------------------


def test_judge_phase_postprocessor_schema_rejects_partial_result(judge_skill) -> None:
    """Tier 2: partial result (score only) fails output_schema validation.

    Confirms that the schema correctly rejects a result that lacks
    criteria_results, passed, phase_name, and summary — mirroring the
    symptom described in B37 W3 S8 (the LLM output was partial).
    Even after the postprocessor adds score, a genuinely incomplete LLM
    output is still caught by the final validation.
    """
    pp = judge_skill.postprocessor
    assert pp is not None

    # Simulate: LLM only emitted 'score' (after postprocessor injects it),
    # missing all other required data fields.
    partial_result = {
        "type": "phase_judgment",
        "data": {
            "score": 0.5,
        },
    }

    validator = jsonschema.Draft7Validator(pp.output_schema)
    errors = list(validator.iter_errors(partial_result))
    assert errors, (
        "Partial result (missing phase_name/passed/criteria_results/summary) "
        "must fail output_schema validation"
    )
    # The error messages should mention the missing required properties.
    messages = " ".join(e.message for e in errors)
    assert "required" in messages.lower() or "is a required property" in messages, (
        f"Expected required-property errors; got: {messages}"
    )
