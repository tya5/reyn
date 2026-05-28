"""Tier 2: FP-0008 PR-O v8 — test_patch sanitization preprocessor.

sandbox_2 v8 calibration retry (2026-05-28) showed 1 instance + 2
rollback events with test_patch git apply reject. Root cause:
SWE-bench input `data.test_patch` may carry line-ending / BOM /
trailing-newline artifacts that vanilla git apply rejects.

Fix shape (structural per [[feedback_root_cause_until_structural_fix]]):
deterministic Python preprocessor step at verify phase entry
sanitizes test_patch BEFORE the LLM issues `git apply`. The step
is declared with ``into: data.test_patch`` so the return value (=
the sanitized string) replaces the LLM-visible field.

This file pins the sanitizer behavior:
  1. CRLF line endings normalized to LF.
  2. UTF-8 BOM at start stripped.
  3. Missing trailing newline added.
  4. Already-clean diff passed through unchanged (= idempotent).
  5. Absent / non-string test_patch yields "" (= verify phase
     handles the empty case via instruction).
  6. Combined artifacts (= BOM + CRLF + no-newline) all handled
     in one pass.

Tier rule discipline: every test docstring opens with Tier 2; no
mocks; no private-state assertions; no format-pinning (= we check
behavior on the returned string, not internal state).
"""
from __future__ import annotations

from reyn.stdlib.skills.swe_bench.sanitize_test_patch import sanitize_test_patch


def test_crlf_normalized_to_lf() -> None:
    """Tier 2: CRLF line endings in test_patch normalize to LF."""
    raw = "diff --git a/x b/x\r\n--- a/x\r\n+++ b/x\r\n@@\r\n-a\r\n+b\r\n"
    out = sanitize_test_patch({"test_patch": raw})
    assert "\r" not in out, (
        f"CRLF normalization failed; output still contains '\\r': {out!r}"
    )


def test_bom_stripped() -> None:
    """Tier 2: UTF-8 BOM at start of test_patch is stripped."""
    raw = "﻿diff --git a/x b/x\n--- a/x\n+++ b/x\n@@\n-a\n+b\n"
    out = sanitize_test_patch({"test_patch": raw})
    assert not out.startswith("﻿")
    assert out.startswith("diff ")


def test_trailing_newline_added() -> None:
    """Tier 2: missing trailing newline is added (= single \\n at end)."""
    raw = "diff --git a/x b/x\n--- a/x\n+++ b/x\n@@\n-a\n+b"
    out = sanitize_test_patch({"test_patch": raw})
    assert out.endswith("\n")


def test_already_clean_diff_passthrough() -> None:
    """Tier 2: a clean diff passes through with no transformations."""
    clean = "diff --git a/x b/x\n--- a/x\n+++ b/x\n@@\n-a\n+b\n"
    out = sanitize_test_patch({"test_patch": clean})
    assert out == clean, (
        f"Clean diff should pass through unchanged; got modification: "
        f"original={clean!r} sanitized={out!r}"
    )


def test_absent_test_patch_yields_empty() -> None:
    """Tier 2: data without test_patch field returns empty string."""
    out = sanitize_test_patch({"instance_id": "t1"})
    assert out == ""


def test_non_string_test_patch_yields_empty() -> None:
    """Tier 2: non-string test_patch (= None / int / dict) returns empty."""
    assert sanitize_test_patch({"test_patch": None}) == ""
    assert sanitize_test_patch({"test_patch": 42}) == ""
    assert sanitize_test_patch({"test_patch": {}}) == ""


def test_empty_string_test_patch_yields_empty() -> None:
    """Tier 2: empty-string test_patch returns empty string (= no-op)."""
    assert sanitize_test_patch({"test_patch": ""}) == ""


def test_combined_artifacts_all_handled() -> None:
    """Tier 2: BOM + CRLF + missing trailing newline all sanitized in one pass."""
    raw = "﻿diff --git a/x b/x\r\n--- a/x\r\n+++ b/x\r\n@@\r\n-a\r\n+b"
    out = sanitize_test_patch({"test_patch": raw})
    assert not out.startswith("﻿")
    assert "\r" not in out
    assert out.endswith("\n")
    assert out.startswith("diff ")


def test_sanitization_idempotent_on_repeated_calls() -> None:
    """Tier 2: sanitizing twice produces the same result as once."""
    raw = "﻿diff --git a/x b/x\r\n--- a/x\r\n+++ b/x\r\n@@\r\n-a\r\n+b"
    once = sanitize_test_patch({"test_patch": raw})
    twice = sanitize_test_patch({"test_patch": once})
    assert twice == once, (
        "Sanitization is not idempotent — applying twice produced different "
        f"output: once={once!r} twice={twice!r}"
    )


def test_lone_cr_normalized() -> None:
    """Tier 2: classic-Mac lone CR line endings normalize to LF."""
    raw = "diff --git a/x b/x\r--- a/x\r+++ b/x\r@@\r-a\r+b\r"
    out = sanitize_test_patch({"test_patch": raw})
    assert "\r" not in out
    assert out.endswith("\n")
