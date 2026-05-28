"""Tier 2: FP-0008 PR-F -- swe_bench skill defect fixes.

Two defects surfaced by sandbox_2 v4 calibration retry (2026-05-28):

1. Placeholder substitution failure -- original setup.md said
   git checkout <base_commit>, which weak LLMs (gemini-2.5-
   flash-lite) emitted as the LITERAL shell cmd string instead of
   substituting the actual SHA value from the input artifact.
   2 of 5 abort instances in v4 retry hit this.

2. Schema invariant gap -- swe_bench_result schema allowed
   tests_passed=true with an empty patch (no edits). v4 retrys
   astropy-14309 finished with that combo, producing a false-positive
   pass_rate of 1/10 masking the true 0/10 outcome.

This file pins:
  (a) setup.md instruction includes an explicit substitute-the-SHA
      directive.
  (b) setup.md shows a concrete hex-SHA example next to git checkout.
  (c) swe_bench_result schema has a oneOf constraint that rejects
      tests_passed=true with an empty patch.
  (d) swe_bench_result schema accepts the two valid shapes:
      tests_passed=false (any patch) and tests_passed=true with a
      non-empty patch.

Tier rule discipline: every test docstring opens with Tier 2; no
mocks; no private-state assertions; no format-pinning. Schema
validation uses jsonschema directly against the loaded schema dict.
"""
from __future__ import annotations

from pathlib import Path

import pytest

_SKILL_ROOT = (
    Path(__file__).parent.parent
    / "src" / "reyn" / "stdlib" / "skills" / "swe_bench"
)


def _load_result_schema() -> dict:
    """Tier 2 helper: load swe_bench_result yaml schema field as a dict."""
    import yaml

    raw = (_SKILL_ROOT / "artifacts" / "swe_bench_result.yaml").read_text(
        encoding="utf-8",
    )
    doc = yaml.safe_load(raw)
    return doc["schema"]


def test_setup_md_includes_substitute_instruction() -> None:
    """Tier 2: setup.md must instruct the LLM to substitute the actual SHA."""
    setup_md = (_SKILL_ROOT / "phases" / "setup.md").read_text(encoding="utf-8")
    assert "substitute" in setup_md.lower(), (
        "setup.md must include an explicit substitute-with-actual-SHA "
        "instruction post-PR-F"
    )


def test_setup_md_shows_concrete_sha_example() -> None:
    """Tier 2: setup.md must show a concrete hex-SHA next to git checkout."""
    setup_md = (_SKILL_ROOT / "phases" / "setup.md").read_text(encoding="utf-8")
    import re
    pattern = re.compile(r"git checkout [0-9a-f]{7,}")
    assert pattern.search(setup_md), (
        "setup.md should include a git-checkout hex-sha as a concrete "
        "example so the LLM understands the substituted shape"
    )


def test_swe_bench_result_schema_rejects_passed_true_with_empty_patch() -> None:
    """Tier 2: schema must reject tests_passed=true with empty patch."""
    import jsonschema

    schema = _load_result_schema()
    invalid_instance = {
        "instance_id": "test__test-1",
        "patch": "",
        "tests_passed": True,
        "attempts": 1,
    }
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=invalid_instance, schema=schema)


def test_swe_bench_result_schema_accepts_passed_true_with_non_empty_patch() -> None:
    """Tier 2: schema accepts tests_passed=true when patch is non-empty."""
    import jsonschema

    schema = _load_result_schema()
    valid_instance = {
        "instance_id": "test__test-1",
        "patch": "diff --git a/x b/x\n--- a/x\n+++ b/x\n@@\n-old\n+new\n",
        "tests_passed": True,
        "attempts": 1,
    }
    jsonschema.validate(instance=valid_instance, schema=schema)


def test_swe_bench_result_schema_accepts_passed_false_with_empty_patch() -> None:
    """Tier 2: schema accepts tests_passed=false with an empty patch."""
    import jsonschema

    schema = _load_result_schema()
    valid_instance = {
        "instance_id": "test__test-1",
        "patch": "",
        "tests_passed": False,
        "attempts": 1,
    }
    jsonschema.validate(instance=valid_instance, schema=schema)


def test_swe_bench_result_schema_accepts_passed_false_with_non_empty_patch() -> None:
    """Tier 2: schema accepts tests_passed=false with a non-empty patch."""
    import jsonschema

    schema = _load_result_schema()
    valid_instance = {
        "instance_id": "test__test-1",
        "patch": "diff --git a/x b/x\n--- a/x\n+++ b/x\n@@\n-old\n+wrong\n",
        "tests_passed": False,
        "attempts": 3,
    }
    jsonschema.validate(instance=valid_instance, schema=schema)


def test_swe_bench_result_schema_carries_oneof_constraint() -> None:
    """Tier 2: structural -- schema dict carries a oneOf constraint."""
    schema = _load_result_schema()
    assert "oneOf" in schema, (
        "swe_bench_result.yaml schema must carry a oneOf constraint "
        "pinning the tests_passed/patch invariant (FP-0008 PR-F)"
    )


def test_report_md_articulates_validation_contract() -> None:
    """Tier 2: report.md tells the LLM about the patch-non-empty-if-passed rule."""
    report_md = (_SKILL_ROOT / "phases" / "report.md").read_text(
        encoding="utf-8",
    )
    text = report_md.lower()
    assert "validation" in text or "schema" in text, (
        "report.md must mention the validation/schema contract so the "
        "LLM understands the patch-non-empty-if-passed rule"
    )
    assert "empty" in text, (
        "report.md must articulate the empty-patch consequence (set "
        "tests_passed=false when diff is empty)"
    )
