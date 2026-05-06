"""Tier 2 invariant tests for improvement_session artifact schema (B10-NEW-1 fix).

Pins the schema invariants introduced in the B10-NEW-1 / B11-R1 fix:
- improvement_session schema must declare _resolved_paths as a known property
  so that _strip_data (artifact_validator.py) preserves it when the LLM emits
  the artifact in the copy_to_work decide turn.
- _strip_data must preserve _resolved_paths when the schema declares it.

Root cause summary (B10-NEW-1):
  The copy_to_work preprocessor injects _resolved_paths into input_artifact.data.
  The LLM is asked to carry it verbatim in its emitted improvement_session artifact.
  However, _strip_data removes any field not in schema.properties.  Without
  _resolved_paths in the schema, _strip_data silently drops it, causing downstream
  phases (run_and_eval, plan_improvements, apply_improvements, finalize) to receive
  a session with no path information.  Those phases then hallucinate paths, leading
  to the /tmp/reyn-workspace (hyphen) vs /tmp/reyn_workspace (underscore) mismatch
  observed in dogfood scenario S1.

Fix:
  1. Added _resolved_paths to improvement_session schema properties (required).
  2. Added CRITICAL carry-through instruction to copy_to_work.md.

These Tier 2 tests pin the schema invariant so that a future refactor cannot
silently remove _resolved_paths from the schema without a test failure.
"""
from __future__ import annotations

from pathlib import Path

import yaml

from reyn.workspace.artifact_validator import _strip_data

_SCHEMA_PATH = (
    Path(__file__).parent.parent
    / "src"
    / "reyn"
    / "stdlib"
    / "skills"
    / "skill_improver"
    / "artifacts"
    / "improvement_session.yaml"
)


def _load_schema() -> dict:
    return yaml.safe_load(_SCHEMA_PATH.read_text(encoding="utf-8"))


# ── (a) schema must declare _resolved_paths in properties ────────────────────


def test_improvement_session_schema_declares_resolved_paths():
    """Tier 2: improvement_session schema must declare _resolved_paths as a known property.

    Invariant: _resolved_paths must be in schema.properties so that _strip_data
    preserves it when the LLM emits the artifact.  Without this, _strip_data
    silently removes it and downstream phases lose all OS-resolved path info.

    This pins the B10-NEW-1 / B11-R1 fix.  If _resolved_paths is removed from
    the schema, run_and_eval receives a session without path info and the LLM
    hallucinates workspace paths.
    """
    raw = _load_schema()
    props = raw["schema"]["properties"]
    assert "_resolved_paths" in props, (
        "improvement_session schema must declare '_resolved_paths' in properties "
        "so that _strip_data preserves it (B10-NEW-1 / B11-R1 fix)"
    )


def test_improvement_session_schema_resolved_paths_is_required():
    """Tier 2: _resolved_paths must be in the required list of improvement_session schema.

    Invariant: marking _resolved_paths required ensures the OS rejects any
    LLM output that omits it — preventing silent path loss.
    """
    raw = _load_schema()
    required = raw["schema"].get("required", [])
    assert "_resolved_paths" in required, (
        "improvement_session schema must include '_resolved_paths' in required "
        "(B10-NEW-1 / B11-R1 fix)"
    )


def test_improvement_session_schema_resolved_paths_sub_properties():
    """Tier 2: _resolved_paths sub-schema must declare all four path sub-fields.

    Invariant: all four path fields (target_skill_path, target_skill_root,
    eval_spec_path, original_skill_root) must be declared as sub-properties so
    that _strip_data does not strip nested path fields.  Each downstream phase
    depends on at least one of these fields.
    """
    raw = _load_schema()
    resolved_schema = raw["schema"]["properties"]["_resolved_paths"]
    sub_props = resolved_schema.get("properties", {})
    expected = {
        "target_skill_path",
        "target_skill_root",
        "eval_spec_path",
        "original_skill_root",
    }
    missing = expected - set(sub_props.keys())
    assert not missing, (
        f"_resolved_paths sub-schema is missing these required path fields: {missing} "
        f"(B10-NEW-1 / B11-R1 fix)"
    )


# ── (b) _strip_data must preserve _resolved_paths when schema declares it ────


def test_strip_data_preserves_resolved_paths_when_declared():
    """Tier 2: _strip_data preserves _resolved_paths when schema declares it.

    Invariant: this is the direct contract test for the root cause mechanism.
    Given an improvement_session schema (from the actual schema file), _strip_data
    must NOT remove _resolved_paths from an artifact that includes it.

    If this test fails, _resolved_paths has been removed from the schema or
    _strip_data's logic has changed in a way that would re-introduce B10-NEW-1.
    """
    raw = _load_schema()
    schema = raw["schema"]

    data = {
        "target_skill": "direct_llm",
        "case_name": "basic",
        "case_input": "hello",
        "phase_criteria": [],
        "model": "standard",
        "max_iterations": 3,
        "score_threshold": 0.85,
        "improvement_focus": "",
        "_resolved_paths": {
            "target_skill_path": ".reyn/skill_improver_work/direct_llm/skill.md",
            "target_skill_root": ".reyn/skill_improver_work/direct_llm",
            "eval_spec_path": "reyn/local/direct_llm/phases/eval.md",
            "original_skill_root": "reyn/local/direct_llm",
        },
    }

    corrections: list[str] = []
    result = _strip_data(data, schema, corrections)

    assert "_resolved_paths" in result, (
        f"_strip_data removed '_resolved_paths' even though it is declared in the schema. "
        f"Corrections applied: {corrections}. "
        f"This would re-introduce B10-NEW-1: downstream phases would lose OS-resolved paths."
    )
    # Verify that none of the corrections mention _resolved_paths being stripped
    stripped_msg = [c for c in corrections if "_resolved_paths" in c and "removed" in c]
    assert not stripped_msg, (
        f"_strip_data reported removing _resolved_paths: {stripped_msg}"
    )


def test_strip_data_removes_resolved_paths_when_not_in_schema():
    """Tier 2: _strip_data removes _resolved_paths when schema does NOT declare it.

    This is the counter-test that demonstrates the pre-fix behavior.
    If _resolved_paths were absent from the schema (as it was before B11-R1),
    _strip_data would have removed it.  This test documents and pins that behavior
    so any future reader understands why the schema fix was necessary.
    """
    # Minimal schema that does NOT include _resolved_paths
    minimal_schema = {
        "type": "object",
        "properties": {
            "target_skill": {"type": "string"},
            "case_name": {"type": "string"},
        },
    }

    data = {
        "target_skill": "direct_llm",
        "case_name": "basic",
        "_resolved_paths": {
            "target_skill_path": ".reyn/skill_improver_work/direct_llm/skill.md",
        },
    }

    corrections: list[str] = []
    result = _strip_data(data, minimal_schema, corrections)

    assert "_resolved_paths" not in result, (
        "_strip_data should remove _resolved_paths when it is not in schema.properties. "
        "This verifies the pre-fix behavior and explains why the schema fix was necessary."
    )
    assert any("_resolved_paths" in c for c in corrections), (
        "_strip_data should record a correction when it removes _resolved_paths"
    )
