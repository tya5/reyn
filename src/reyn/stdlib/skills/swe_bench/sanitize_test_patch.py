"""Deterministic test_patch sanitization for swe_bench verify phase.

FP-0008 PR-N15 (= workspace passthrough) + PR-O v8 (= line-ending / BOM /
trailing-newline normalization).

## Workspace-passthrough design (PR-N15)

The verify phase preprocessor runs this function AFTER a ``run_op: file.read``
step that reads the original ``swe_bench_input`` artifact from the workspace:

    ``.reyn/artifacts/swe_bench/_input/v01_swe_bench_input.json``

The read result lands at ``data._input_raw`` (inside the full artifact dict)
as::

    {"status": "ok", "content": "<JSON string>", ...}

This function reads ``test_patch`` from that JSON string, parses it, and
sanitizes it.  The apply-phase LLM no longer echoes ``test_patch`` — the
workspace file is the deterministic source (P5: Workspace is the single
source of truth).

## Fallback

When ``data._input_raw`` is absent or its ``content`` is not valid JSON (e.g.
in unit tests that inject the verify phase input directly), the function
falls back to reading ``test_patch`` from the artifact in two structural
shapes:

- **Full artifact** (runtime shape):
  ``{"type": "...", "data": {"test_patch": "..."}}``
- **Flat dict** (unit-test direct-call shape):
  ``{"test_patch": "..."}``

## Sanitization steps (PR-O v8)

All steps are conservative and idempotent — valid diffs pass through unchanged:

1. Remove UTF-8 BOM if present at the start.
2. Normalize line endings to LF (= strip CR characters).
3. Ensure the patch ends with a single trailing newline.

Returns the sanitized string.  When all sources are absent/non-string,
returns an empty string (the verify-phase instruction handles the empty case
explicitly).

Author judgment per [[feedback_root_cause_until_structural_fix]]:
deterministic preprocessor > LLM-driven prompt-side normalization
(= the LLM cannot reliably perform byte-level diff cleanup without
mistakes; OS preprocessor is structurally sound).
"""
from __future__ import annotations

import json
from typing import Any, Mapping


def sanitize_test_patch(data: Mapping[str, Any]) -> str:
    """Return a normalized ``test_patch`` string.

    Source priority (P5 workspace passthrough):
      1. ``data._input_raw.content`` (via artifact's data dict) — JSON of the
         original swe_bench_input artifact, injected by the preceding
         ``run_op: file.read`` step.  Parsed to extract ``test_patch`` from
         the artifact's ``data`` field.
      2. ``data.test_patch`` from the artifact's ``data`` dict (full runtime
         artifact shape: ``{"type": "...", "data": {"test_patch": "..."}}``)
      3. Top-level ``test_patch`` on the passed-in dict (flat unit-test shape:
         ``{"test_patch": "..."}``)

    Sanitization steps (all idempotent):
      1. Remove UTF-8 BOM if present at the start.
      2. Normalize line endings to LF (= strip CR characters).
      3. Ensure the patch ends with a single trailing newline.

    Returns the sanitized string. When all sources are absent or non-string,
    returns an empty string (= the preprocessor ``into:`` target lands as
    empty; ``verify.md``'s instruction handles the empty case explicitly).
    """
    raw: Any = None

    # Extract the inner data dict from the full artifact (runtime shape).
    inner_data: Any = data.get("data") or {}

    # Priority 1: workspace passthrough — run_op file.read result
    # The preceding run_op step reads the workspace _input artifact and places
    # its result at data._input_raw. The result shape is:
    # {"status": "ok", "content": "<JSON of swe_bench_input artifact>", ...}
    input_raw = inner_data.get("_input_raw") if isinstance(inner_data, dict) else None
    if isinstance(input_raw, dict):
        content = input_raw.get("content")
        if isinstance(content, str) and content.strip():
            try:
                parsed = json.loads(content)
                # Workspace file is the full artifact:
                # {"type": "swe_bench_input", "data": {"test_patch": ...}}
                raw = (parsed.get("data") or {}).get("test_patch")
            except (json.JSONDecodeError, AttributeError):
                pass

    # Priority 2: data.test_patch from inner data dict (runtime artifact shape)
    if not isinstance(raw, str) or not raw:
        if isinstance(inner_data, dict):
            raw = inner_data.get("test_patch")

    # Priority 3: top-level test_patch (flat unit-test direct-call shape)
    if not isinstance(raw, str) or not raw:
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
