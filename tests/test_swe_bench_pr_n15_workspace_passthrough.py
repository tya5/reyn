"""Tier 2: FP-0008 PR-N15 — test_patch deterministic workspace passthrough.

Root cause: `apply.md` instructed the apply-phase LLM to echo `test_patch`
verbatim from the plan input into the `apply_state` output artifact.  Weak
models (gemini-flash-lite) dropped or nulled the field, causing schema
validation failure at the verify-phase entry.

Fix shape (PR-N15):
  - `test_patch` removed from `apply_state` schema (no more LLM echo).
  - `apply.md` "Carry test_patch" instruction removed.
  - `verify.md` preprocessor gains a `run_op: file.read` step that reads
    the workspace-stored ``_input`` artifact (= the original swe_bench_input)
    before the python sanitizer step.
  - `sanitize_test_patch.py` updated to read from the workspace-injected
    ``data._input_raw.content`` (Priority 1), falling back to
    ``data.test_patch`` in the artifact's data dict (Priority 2) or top-level
    flat dict (Priority 3, unit-test compat).

This file pins:
  (a) `apply_state` schema does NOT include `test_patch` as a required field.
  (b) `apply.md` does NOT contain the "Carry test_patch" instruction.
  (c) `verify.md` preprocessor contains a `run_op` step before the python
      step that reads the workspace input file.
  (d) `sanitize_test_patch` extracts test_patch from the workspace-injected
      ``_input_raw.content`` payload (Priority 1 path = the fix for 13977).
  (e) `sanitize_test_patch` handles the full runtime artifact shape
      ``{"type": "apply_state", "data": {"test_patch": "..."}}`` correctly
      (Priority 2 path).
  (f) `sanitize_test_patch` returns empty string when workspace payload is
      absent and test_patch is missing (existing behavior preserved).
  (g) Regression guard: LLM-null test_patch in artifact + workspace content
      present → workspace value wins (= the 13977 failure mode is fixed).

Tier rule discipline: every test docstring opens with Tier 2; no mocks; no
private-state assertions; no format-pinning.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

_SKILL_ROOT = (
    Path(__file__).parent.parent
    / "src" / "reyn" / "stdlib" / "skills" / "swe_bench"
)

# ── (a) apply_state schema: test_patch not in required ───────────────────────


def test_apply_state_schema_no_test_patch_required() -> None:
    """Tier 2: apply_state.yaml must not require test_patch.

    PR-N15 removes test_patch from apply_state so the apply-phase LLM
    is never asked to echo the large diff string.  A weak model that
    cannot faithfully copy test_patch will no longer cause a schema
    validation failure.
    """
    raw = (_SKILL_ROOT / "artifacts" / "apply_state.yaml").read_text(encoding="utf-8")
    doc = yaml.safe_load(raw)
    required = doc.get("schema", {}).get("required", [])
    assert "test_patch" not in required, (
        "apply_state.schema.required must not include 'test_patch' after PR-N15. "
        f"Actual required: {required}"
    )


def test_apply_state_schema_no_test_patch_property() -> None:
    """Tier 2: apply_state.yaml must not define a test_patch property.

    Removes the field entirely so the LLM is not tempted to fill it
    (a defined-but-not-required field still leaks through the LLM prompt).
    """
    raw = (_SKILL_ROOT / "artifacts" / "apply_state.yaml").read_text(encoding="utf-8")
    doc = yaml.safe_load(raw)
    properties = doc.get("schema", {}).get("properties", {})
    assert "test_patch" not in properties, (
        "apply_state.schema.properties must not include 'test_patch' after PR-N15. "
        f"Actual properties: {sorted(properties.keys())}"
    )


# ── (b) apply.md: no "Carry test_patch" instruction ──────────────────────────


def test_apply_md_no_carry_test_patch_instruction() -> None:
    """Tier 2: apply.md must not contain the 'Carry test_patch' instruction.

    PR-N15 removes the line that asked the LLM to echo test_patch so
    the apply phase has no knowledge of test_patch propagation.
    """
    apply_md = (_SKILL_ROOT / "phases" / "apply.md").read_text(encoding="utf-8")
    # The old instruction contained the words "Carry" and "test_patch"
    # together.  Both must no longer co-occur.
    assert "Carry" not in apply_md or "test_patch" not in apply_md or (
        "Carry" not in apply_md and "test_patch" not in apply_md
    ), (
        "apply.md must not contain 'Carry ... test_patch' instruction after PR-N15"
    )
    # Stronger check: the specific instruction phrase must be absent
    assert "Carry `test_patch`" not in apply_md, (
        "apply.md still contains the old 'Carry `test_patch`' instruction — "
        "this must be removed in PR-N15"
    )


# ── (c) verify.md preprocessor: run_op step reads workspace input ────────────


def test_verify_md_preprocessor_has_run_op_before_python() -> None:
    """Tier 2: verify.md preprocessor must contain a run_op step before python.

    PR-N15 adds a run_op: file.read step as the FIRST preprocessor step to
    read the workspace _input artifact.  This must appear BEFORE the python
    sanitizer step so the sanitizer can consume it.
    """
    verify_md = (_SKILL_ROOT / "phases" / "verify.md").read_text(encoding="utf-8")
    run_op_pos = verify_md.find("type: run_op")
    python_pos = verify_md.find("type: python")
    assert run_op_pos != -1, (
        "verify.md preprocessor must contain a 'type: run_op' step after PR-N15"
    )
    assert python_pos != -1, (
        "verify.md preprocessor must still contain a 'type: python' step"
    )
    assert run_op_pos < python_pos, (
        "The run_op step must appear BEFORE the python step in verify.md "
        "so the workspace _input is injected before sanitize_test_patch runs"
    )


def test_verify_md_preprocessor_reads_workspace_input_file() -> None:
    """Tier 2: verify.md run_op step must target the workspace _input artifact.

    The workspace-stored path for the swe_bench entry-phase input is
    deterministic: `.reyn/artifacts/swe_bench/_input/v01_swe_bench_input.json`.
    PR-N15 pins this path in the run_op so test_patch is always read from
    the workspace rather than being LLM-echoed.
    """
    verify_md = (_SKILL_ROOT / "phases" / "verify.md").read_text(encoding="utf-8")
    assert "swe_bench/_input/v01_swe_bench_input.json" in verify_md, (
        "verify.md must reference the workspace _input artifact path "
        "'.reyn/artifacts/swe_bench/_input/v01_swe_bench_input.json' in the "
        "run_op preprocessor step"
    )


def test_verify_md_run_op_injects_into_input_raw() -> None:
    """Tier 2: verify.md run_op step must inject result into data._input_raw.

    The sanitize_test_patch function reads from data._input_raw.content
    (Priority 1).  The run_op's into: field must target data._input_raw.
    """
    verify_md = (_SKILL_ROOT / "phases" / "verify.md").read_text(encoding="utf-8")
    assert "into: data._input_raw" in verify_md, (
        "verify.md run_op step must use 'into: data._input_raw' so the "
        "sanitize_test_patch function can read the workspace artifact content"
    )


# ── (d) sanitizer: Priority 1 — workspace _input_raw path ───────────────────


def _make_workspace_artifact(test_patch: str) -> dict:
    """Build the full artifact dict as the preprocessor sees it after run_op.

    The run_op step injects the workspace file read result at data._input_raw.
    The read result shape is {"status": "ok", "content": "<JSON>", ...}.
    The JSON is the full swe_bench_input artifact.
    """
    swe_bench_input_artifact = {
        "type": "swe_bench_input",
        "data": {
            "instance_id": "test__test-1",
            "repo": "test/test",
            "base_commit": "abc123",
            "problem_statement": "Fix a bug",
            "test_patch": test_patch,
        },
    }
    return {
        "type": "apply_state",
        "data": {
            "instance_id": "test__test-1",
            "files_edited": ["foo.py"],
            "attempt": 1,
            "_input_raw": {
                "status": "ok",
                "content": json.dumps(swe_bench_input_artifact),
            },
        },
    }


def test_sanitizer_reads_from_workspace_input_raw() -> None:
    """Tier 2: sanitizer extracts test_patch from data._input_raw.content.

    This is the PR-N15 primary fix path (Priority 1).  When the run_op
    step has injected _input_raw.content with the workspace JSON, the
    sanitizer reads test_patch from there — not from the apply LLM output.
    """
    from reyn.stdlib.skills.swe_bench.sanitize_test_patch import sanitize_test_patch

    patch_str = "diff --git a/x b/x\n--- a/x\n+++ b/x\n@@\n-old\n+new\n"
    artifact = _make_workspace_artifact(patch_str)
    result = sanitize_test_patch(artifact)
    assert result == patch_str, (
        f"Sanitizer must read test_patch from data._input_raw.content. "
        f"Expected: {patch_str!r}, got: {result!r}"
    )


def test_sanitizer_workspace_path_normalizes_crlf() -> None:
    """Tier 2: sanitizer normalizes CRLF in test_patch from workspace source."""
    from reyn.stdlib.skills.swe_bench.sanitize_test_patch import sanitize_test_patch

    raw = "diff --git a/x b/x\r\n--- a/x\r\n+++ b/x\r\n@@\r\n-a\r\n+b\r\n"
    artifact = _make_workspace_artifact(raw)
    result = sanitize_test_patch(artifact)
    assert "\r" not in result, (
        "CRLF normalization must apply even when reading from workspace source"
    )


# ── (e) sanitizer: Priority 2 — full runtime artifact shape ─────────────────


def test_sanitizer_reads_from_full_artifact_data_dict() -> None:
    """Tier 2: sanitizer reads test_patch from artifact['data']['test_patch'].

    Priority 2 path: when _input_raw is absent but test_patch is present
    in the artifact's inner data dict (full runtime artifact shape).  This
    was the broken path before PR-N15 — the old code did data.get('test_patch')
    where data was the FULL artifact dict, looking at the wrong level.
    """
    from reyn.stdlib.skills.swe_bench.sanitize_test_patch import sanitize_test_patch

    patch_str = "diff --git a/x b/x\n--- a/x\n+++ b/x\n@@\n-old\n+new\n"
    # Full artifact shape without _input_raw — must still work via fallback
    artifact = {
        "type": "apply_state",
        "data": {
            "instance_id": "test__test-1",
            "files_edited": ["foo.py"],
            "attempt": 1,
            "test_patch": patch_str,
        },
    }
    result = sanitize_test_patch(artifact)
    assert result == patch_str, (
        f"Sanitizer must read test_patch from artifact['data']['test_patch']. "
        f"Expected: {patch_str!r}, got: {result!r}"
    )


# ── (f) sanitizer: empty when all sources absent ─────────────────────────────


def test_sanitizer_empty_when_no_sources() -> None:
    """Tier 2: sanitizer returns empty string when all test_patch sources absent.

    Both _input_raw and test_patch missing → empty string (verify phase
    instruction handles this case explicitly).
    """
    from reyn.stdlib.skills.swe_bench.sanitize_test_patch import sanitize_test_patch

    artifact = {
        "type": "apply_state",
        "data": {
            "instance_id": "test__test-1",
            "files_edited": ["foo.py"],
            "attempt": 1,
        },
    }
    assert sanitize_test_patch(artifact) == ""


def test_sanitizer_empty_when_workspace_content_invalid_json() -> None:
    """Tier 2: sanitizer returns empty string when _input_raw.content is not JSON.

    on_error: empty in the run_op step can produce _input_raw=None or
    an empty content field.  The sanitizer must not raise in these cases.
    """
    from reyn.stdlib.skills.swe_bench.sanitize_test_patch import sanitize_test_patch

    artifact = {
        "type": "apply_state",
        "data": {
            "instance_id": "test__test-1",
            "files_edited": [],
            "attempt": 1,
            "_input_raw": {"status": "error", "content": "NOT JSON {{{"},
        },
    }
    # Should not raise; falls through to missing test_patch → empty string
    result = sanitize_test_patch(artifact)
    assert result == ""


# ── (g) regression: LLM-null + workspace present → workspace wins ────────────


def test_sanitizer_workspace_wins_over_null_artifact_test_patch() -> None:
    """Tier 2: regression guard for 13977 failure mode.

    The original bug: apply LLM emitted test_patch=null → schema fail.
    After PR-N15, apply_state no longer has test_patch at all, and the
    verify preprocessor injects test_patch from the workspace.

    This test simulates the scenario where _input_raw is present (from
    workspace passthrough) and test_patch in the inner data is absent/null
    (= what a weak-model apply-phase output would look like if test_patch
    were still in the schema).  The workspace value must win.
    """
    from reyn.stdlib.skills.swe_bench.sanitize_test_patch import sanitize_test_patch

    real_patch = "diff --git a/x b/x\n--- a/x\n+++ b/x\n@@\n-old\n+new\n"
    artifact = {
        "type": "apply_state",
        "data": {
            "instance_id": "test__test-1",
            "files_edited": ["foo.py"],
            "attempt": 1,
            # test_patch in the artifact data is null (= weak LLM dropped it)
            "test_patch": None,
            # But _input_raw has the real value from the workspace
            "_input_raw": {
                "status": "ok",
                "content": json.dumps({
                    "type": "swe_bench_input",
                    "data": {
                        "instance_id": "test__test-1",
                        "test_patch": real_patch,
                    },
                }),
            },
        },
    }
    result = sanitize_test_patch(artifact)
    assert result == real_patch, (
        "Workspace _input_raw must win over null test_patch in artifact. "
        f"Expected workspace value: {real_patch!r}, got: {result!r}"
    )
