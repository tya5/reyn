"""Deterministic test_patch sanitization for swe_bench verify phase.

FP-0008 PR-O v8 (= sandbox_2 v8 calibration retry 2026-05-28 root
cause): SWE-bench input ``data.test_patch`` may carry artifacts from
upstream collection / serialization that cause ``git apply`` to
reject the patch:

- **Line endings**: CRLF in patch content while target files have
  LF (or vice versa) — the diff context lines don't byte-match.
- **BOM**: a UTF-8 BOM at the start of the patch string makes the
  first diff header unparseable.
- **Trailing-newline drift**: missing final newline causes some git
  versions to reject the last hunk.

This module is a ``safe``-mode Python preprocessor step that runs
BEFORE the LLM enters the verify phase. The verify-phase
preprocessor writes the return value into ``data.test_patch`` via
``into:``, replacing the LLM-visible field with the deterministically-
normalized version.

Sanitization is conservative + idempotent: valid diffs pass through
unchanged; only the specific artifact patterns above are normalized.

Author judgment per [[feedback_root_cause_until_structural_fix]]:
deterministic preprocessor > LLM-driven prompt-side normalization
(= the LLM cannot reliably perform byte-level diff cleanup without
mistakes; OS preprocessor is structurally sound).
"""
from __future__ import annotations

from typing import Any, Mapping


def sanitize_test_patch(data: Mapping[str, Any]) -> str:
    """Return a normalized ``data.test_patch`` string.

    Steps (all idempotent):
      1. Remove UTF-8 BOM if present at the start.
      2. Normalize line endings to LF (= strip CR characters).
      3. Ensure the patch ends with a single trailing newline.

    Returns the sanitized string. When ``test_patch`` is absent or
    non-string, returns an empty string (= the preprocessor ``into:``
    target lands as empty; ``verify.md``'s instruction handles the
    empty case explicitly).
    """
    raw = data.get("test_patch")
    if not isinstance(raw, str) or not raw:
        return ""

    # 1. BOM
    if raw.startswith("﻿"):
        raw = raw[1:]

    # 2. Line endings
    if "\r" in raw:
        raw = raw.replace("\r\n", "\n").replace("\r", "\n")

    # 3. Trailing newline
    if not raw.endswith("\n"):
        raw = raw + "\n"

    return raw
