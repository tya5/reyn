"""Tier 2: FP-0008 PR-G -- explore/plan input artifact field access.

Defect surfaced by sandbox_2 v5 calibration retry (2026-05-28): 3 of 8
aborts in v5 hit explore/plan with reasons like:

  "The problem_statement and test_patch are missing"
  "Could not read the exploration summary or test patch"
  "Act turns limit reached before reading problem statement"

Root cause: weak LLMs (gemini-2.5-flash-lite) did not navigate the
input artifact's `data.*` fields without explicit prompt scaffolding.
The original explore.md/plan.md said "Read `problem_statement` from
the input artifact" -- template-style reference that the weak LLM
treated as ambiguous and aborted on.

This file pins:
  (a) explore.md includes an explicit "Where to find the input
      fields" section showing the artifact data shape.
  (b) explore.md instructs reading `data.problem_statement` /
      `data.test_patch` directly from the prompt artifact section
      (= no need to grep/probe).
  (c) explore.md warns against aborting with "field missing" before
      reading the artifact.
  (d) plan.md instructs reading the exploration artifact's `data`
      fields + accessing `data.failure_summary` on verify_state.

The rule shape is positive (= required content), so future authors
can rephrase as long as the substance is present.

Tier rule discipline: every test docstring opens with Tier 2; no
mocks; no private-state assertions; no format-pinning.
"""
from __future__ import annotations

from pathlib import Path

_SKILL_ROOT = (
    Path(__file__).parent.parent
    / "src" / "reyn" / "stdlib" / "skills" / "swe_bench"
)


def _read_phase(name: str) -> str:
    return (_SKILL_ROOT / "phases" / f"{name}.md").read_text(encoding="utf-8")


# Section 1: explore.md field-access instructions ----------------------------


def test_explore_md_shows_input_artifact_shape() -> None:
    """Tier 2: explore.md shows the input artifact data shape inline."""
    text = _read_phase("explore")
    # The artifact-shape block should appear (= JSON example with the
    # six data fields). Soft check: confirm the section header + the
    # six field names appear.
    assert "Where to find the input fields" in text or "input artifact" in text.lower(), (
        "explore.md must include an explicit section telling the LLM "
        "where the input fields live"
    )
    for field in ("instance_id", "repo", "base_commit", "problem_statement",
                  "hints_text", "test_patch"):
        assert field in text, (
            f"explore.md must reference the {field!r} input field "
            f"explicitly (= weak LLM navigation aid)"
        )


def test_explore_md_uses_data_dot_prefix_for_field_access() -> None:
    """Tier 2: explore.md uses `data.<field>` form to anchor the access pattern."""
    text = _read_phase("explore")
    # Look for at least one explicit `data.problem_statement` or
    # `data.test_patch` reference. The dot-prefix form tells the LLM
    # the access path; bare `problem_statement` was ambiguous.
    assert "data.problem_statement" in text or "data.test_patch" in text, (
        "explore.md must use `data.<field>` access syntax (= explicit "
        "path) at least once so weak LLMs can pattern-match the field "
        "location in the input artifact"
    )


def test_explore_md_warns_against_premature_abort() -> None:
    """Tier 2: explore.md tells the LLM NOT to abort with `field missing`.

    The v5 retry aborts were of the shape `"problem_statement and
    test_patch are missing"` -- weak LLMs giving up before reading the
    artifact block. The instruction must call this out so the LLM
    re-reads the prompt rather than aborting.
    """
    text = _read_phase("explore").lower()
    # Soft check: confirm both ingredients (= "abort" warning + the
    # specific concern about pre-read abort) appear somewhere.
    assert "abort" in text, (
        "explore.md should mention the abort anti-pattern so the LLM "
        "is steered away from premature 'fields missing' aborts"
    )
    assert "missing" in text or "before reading" in text, (
        "explore.md should articulate that fields ARE in the prompt "
        "(= do not abort before reading)"
    )


# Section 2: plan.md field-access instructions -------------------------------


def test_plan_md_distinguishes_exploration_vs_verify_state() -> None:
    """Tier 2: plan.md tells the LLM which fields to read per input type."""
    text = _read_phase("plan")
    # plan.md accepts two input artifact types; both branches must be
    # described.
    assert "exploration" in text and "verify_state" in text, (
        "plan.md must describe both `exploration` and `verify_state` "
        "input branches so the LLM dispatches on the artifact type"
    )


def test_plan_md_references_failure_summary_on_verify_state() -> None:
    """Tier 2: plan.md instructs reading `data.failure_summary` on verify_state input."""
    text = _read_phase("plan")
    assert "failure_summary" in text, (
        "plan.md must reference `failure_summary` (= the field carrying "
        "the prior-attempt diagnostic on verify_state inputs)"
    )


def test_plan_md_warns_against_premature_abort() -> None:
    """Tier 2: plan.md tells the LLM NOT to abort before reading the input."""
    text = _read_phase("plan").lower()
    # Same anti-pattern as explore.md: weak LLMs aborted with
    # "exploration missing" before reading.
    assert "abort" in text or "missing" in text, (
        "plan.md should articulate that the input artifact's data IS "
        "present in the prompt (= no premature abort)"
    )


# Section 3: schema fields match the documented shape ------------------------


def test_input_schema_lists_documented_fields() -> None:
    """Tier 2: swe_bench_input.yaml schema matches the fields documented in explore.md.

    Catches drift between the schema and the explore.md "Where to find
    the input fields" section. If the schema gains/loses a field, the
    explore.md docs section must follow (= or vice versa).
    """
    import yaml

    raw = (_SKILL_ROOT / "artifacts" / "swe_bench_input.yaml").read_text(
        encoding="utf-8",
    )
    doc = yaml.safe_load(raw)
    schema_fields = set(doc["schema"]["properties"].keys())
    expected = {
        "instance_id", "repo", "base_commit",
        "problem_statement", "hints_text", "test_patch",
    }
    assert schema_fields == expected, (
        f"swe_bench_input schema fields drifted from documented set. "
        f"Schema has: {schema_fields}; docs reference: {expected}. "
        f"Update either schema OR explore.md so they stay synced."
    )
