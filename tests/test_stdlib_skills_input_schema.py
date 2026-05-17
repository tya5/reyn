"""Tier 2: OS invariant tests — stdlib skills expose non-empty input_schema via catalogue.

B36/B37/B38 retros flagged a hot-list coverage gap: 4 stdlib skills were absent
from the ARS / hot-list because their catalogue entry had no ``input_schema``
(= the D2-full condition ``properties non-empty`` skipped them).

Root causes (B39 investigation):
- ``skill__direct_llm`` and ``skill__read_local_files``: their entry phases take
  ``user_message`` as input, but ``user_message.yaml`` lived only in
  ``src/reyn/stdlib/artifacts/``, not in each skill's own ``artifacts/`` dir.
  ``_extract_skill_input_hint`` only searches the skill-local artifacts dir,
  so it found nothing.
- ``skill__index_docs`` and ``skill__eval``: already had their input artifact in
  the skill-local dir and were already working; included in the test suite to
  make regression detection explicit.

Fix (B39): added ``user_message.yaml`` to ``direct_llm/artifacts/`` and
``read_local_files/artifacts/``.  No OS code changed.

All tests use ``enumerate_available_skills`` (the public catalogue API) — not
private attributes or mock objects.
"""
from __future__ import annotations

import pytest

from reyn.chat.session import enumerate_available_skills

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
# skill__read_local_files
# ---------------------------------------------------------------------------


def test_read_local_files_input_schema_non_empty():
    """Tier 2: read_local_files catalogue entry exposes non-empty input_schema.properties.

    read_local_files entry phase (decide_files) takes user_message.  Before
    B39 the fix, user_message.yaml was absent from the skill-local artifacts/
    dir, so _extract_skill_input_hint returned no input_schema and the skill
    was invisible to the ARS / hot-list alias builder.
    """
    entry = _skill_entry("read_local_files")
    assert "input_schema" in entry, (
        "read_local_files catalogue entry must have input_schema (B39 fix)"
    )
    props = entry["input_schema"].get("properties") or {}
    assert props, "read_local_files input_schema.properties must be non-empty"


def test_read_local_files_input_schema_fields():
    """Tier 2: read_local_files input_schema mirrors the user_message artifact (text field).

    The entry phase (decide_files) reads input_artifact.data.text.  The
    catalogue must expose ``text`` so callers know the canonical field name.
    """
    entry = _skill_entry("read_local_files")
    props = (entry.get("input_schema") or {}).get("properties") or {}
    assert "text" in props, (
        f"read_local_files input_schema must contain 'text' field; got {sorted(props)}"
    )


# ---------------------------------------------------------------------------
# skill__direct_llm
# ---------------------------------------------------------------------------


def test_direct_llm_input_schema_non_empty():
    """Tier 2: direct_llm catalogue entry exposes non-empty input_schema.properties.

    direct_llm entry phase (respond) takes user_message.  Before B39 the fix,
    user_message.yaml was absent from the skill-local artifacts/ dir, causing
    the same coverage gap as read_local_files.
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
