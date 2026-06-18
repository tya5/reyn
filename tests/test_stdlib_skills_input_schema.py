"""Tier 2: OS invariant tests — stdlib skills expose non-empty input_schema via catalogue.

B36/B37/B38 retros flagged a hot-list coverage gap: stdlib skills were absent
from the ARS / hot-list because their catalogue entry had no ``input_schema``
(= the D2-full condition ``properties non-empty`` skipped them).

Root causes (B39 + B46 investigation):
- ``skill__direct_llm``: entry phase takes ``user_message`` as input, but
  ``user_message.yaml`` lived only in ``src/reyn/stdlib/artifacts/``, not in
  the skill's own ``artifacts/`` dir. Initial B39 fix copied the file
  skill-side; B46 widened the fix so ``_extract_skill_input_hint`` falls
  back to the stdlib shared artifacts dir whenever a skill-local file is
  absent (= covers every shared-input skill, not just the originals).
- ``skill__index_docs`` and ``skill__eval``: already had their input artifact in
  the skill-local dir and were already working; included in the test suite to
  make regression detection explicit.

All tests use ``enumerate_available_skills`` (the public catalogue API) — not
private attributes or mock objects.
"""
from __future__ import annotations

import pytest

from reyn.runtime.session import enumerate_available_skills

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _skill_entry(name: str) -> dict:
    """Return the catalogue entry for *name*, skip the test if skill is absent."""
    skills = enumerate_available_skills(exclude=set())
    by_name = {s["name"]: s for s in skills if isinstance(s, dict)}
    if name not in by_name:
        pytest.skip(f"stdlib skill '{name}' not found in catalogue — skip")
    return by_name[name]


# ---------------------------------------------------------------------------
# skill__index_docs
# ---------------------------------------------------------------------------


def test_index_docs_input_schema_non_empty():
    """Tier 2: index_docs catalogue entry exposes non-empty input_schema.properties.

    index_docs entry phase (strategy) takes index_docs_input with fields
    source / path / description / mode.  The catalogue must expose these so
    the hot-list alias skill__index_docs has a real parameters schema.
    """
    entry = _skill_entry("index_docs")
    assert "input_schema" in entry, (
        "index_docs catalogue entry must have input_schema (B36/B37/B38 hot-list gap)"
    )
    props = entry["input_schema"].get("properties") or {}
    assert props, "index_docs input_schema.properties must be non-empty"


def test_index_docs_input_schema_fields():
    """Tier 2: index_docs input_schema mirrors the index_docs_input artifact fields.

    The entry phase (strategy) consumes: source, path, description, mode.
    These must all appear in the catalogue input_schema so the LLM sees
    canonical field names before calling skill__index_docs.
    """
    entry = _skill_entry("index_docs")
    props = (entry.get("input_schema") or {}).get("properties") or {}
    expected_fields = {"source", "path", "description"}  # mode is optional
    missing = expected_fields - set(props.keys())
    assert not missing, (
        f"index_docs input_schema is missing required fields: {missing!r}"
    )


# ---------------------------------------------------------------------------
# skill__direct_llm
# ---------------------------------------------------------------------------


def test_direct_llm_input_schema_non_empty():
    """Tier 2: direct_llm catalogue entry exposes non-empty input_schema.properties.

    direct_llm entry phase (respond) takes user_message.  Before B39 the fix,
    user_message.yaml was absent from the skill-local artifacts/ dir, causing
    the same coverage gap that motivated B39 / B46 fixes.
    """
    entry = _skill_entry("direct_llm")
    assert "input_schema" in entry, (
        "direct_llm catalogue entry must have input_schema (B39 fix)"
    )
    props = entry["input_schema"].get("properties") or {}
    assert props, "direct_llm input_schema.properties must be non-empty"


def test_direct_llm_input_schema_fields():
    """Tier 2: direct_llm input_schema mirrors the user_message artifact (text field).

    The entry phase (respond) reads input_artifact.data.text.  The catalogue
    must expose ``text`` so callers know the canonical field name.
    """
    entry = _skill_entry("direct_llm")
    props = (entry.get("input_schema") or {}).get("properties") or {}
    assert "text" in props, (
        f"direct_llm input_schema must contain 'text' field; got {sorted(props)}"
    )


# ---------------------------------------------------------------------------
# skill__eval
# ---------------------------------------------------------------------------


def test_eval_input_schema_non_empty():
    """Tier 2: eval catalogue entry exposes non-empty input_schema.properties.

    eval entry phase (run_target) takes eval_case_input with fields
    case_name / case_input / spec_path / target_skill_path / phase_criteria.
    These are in the skill-local artifacts/ dir, so this worked pre-B39.
    Test is included for explicit regression coverage.
    """
    entry = _skill_entry("eval")
    assert "input_schema" in entry, (
        "eval catalogue entry must have input_schema"
    )
    props = entry["input_schema"].get("properties") or {}
    assert props, "eval input_schema.properties must be non-empty"


def test_eval_input_schema_fields():
    """Tier 2: eval input_schema mirrors the eval_case_input artifact fields.

    The entry phase (run_target) consumes: case_name, case_input, spec_path,
    target_skill_path, phase_criteria.  These must appear in the catalogue
    schema.
    """
    entry = _skill_entry("eval")
    props = (entry.get("input_schema") or {}).get("properties") or {}
    expected_fields = {"case_name", "case_input", "spec_path", "target_skill_path", "phase_criteria"}
    missing = expected_fields - set(props.keys())
    assert not missing, (
        f"eval input_schema is missing required fields: {missing!r}"
    )
