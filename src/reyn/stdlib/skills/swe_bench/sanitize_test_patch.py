"""Deterministic test_patch sanitization for swe_bench verify phase.

FP-0008 PR-N15 (= workspace passthrough) + PR-O v8 (= line-ending / BOM /
trailing-newline normalization).

## Deterministic entry-input passthrough (PR-N15 + #1115 Stage 0)

The OS injects the skill's original entry artifact (the ``swe_bench_input``)
at the reserved top-level ``_skill_input`` binding before the verify
preprocessor runs.  This function reads ``test_patch`` from
``_skill_input.data.test_patch`` and sanitizes it.  The apply-phase LLM no
longer echoes ``test_patch`` — the OS-held entry input is the deterministic
source (P5: Workspace is the single source of truth) and is never LLM-mutated.

#1115 Stage 0 removed the prior ``run_op: file.read`` of
``.reyn/artifacts/swe_bench/_input/v01_swe_bench_input.json`` — that magic path
coupled the read to ``base_dir``, which breaks once the repo filesystem routes
through a backend.  The ``_skill_input`` binding is base_dir-independent.

## Fallback

When ``_skill_input`` is absent, the function falls back to the legacy
``data._input_raw`` shape (a ``run_op: file.read`` result —
``{"status": "ok", "content": "<JSON string>", ...}`` — retained for unit
tests that inject it), then to reading ``test_patch`` from the artifact in two
structural shapes:

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

    Source priority (P5 deterministic entry-input passthrough):
      0. ``_skill_input.data.test_patch`` — the OS-injected original entry
         artifact (#1115 Stage 0), placed at the top-level ``_skill_input``
         binding before the preprocessor runs.  Deterministic, never
         LLM-mutated; replaces the prior base_dir-coupled file.read.
      1. ``data._input_raw.content`` (via artifact's data dict) — JSON of the
         original swe_bench_input artifact, from a ``run_op: file.read`` step.
         Retained for back-compat with unit tests injecting ``_input_raw``.
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

    # Priority 0 (#1115 Stage 0): OS-injected entry input. The OS places the
    # skill's original entry artifact at the top-level `_skill_input` binding
    # (sibling of `data`) before the preprocessor runs. Shape:
    # {"type": "swe_bench_input", "data": {"test_patch": "..."}}. This is the
    # deterministic P5 source — never LLM-mutated — replacing the prior
    # `.reyn/artifacts/...` file.read (which coupled the read to base_dir).
    skill_input = data.get("_skill_input")
    if isinstance(skill_input, dict):
        si_data = skill_input.get("data")
        if isinstance(si_data, dict):
            raw = si_data.get("test_patch")

    # Priority 1 (back-compat): workspace passthrough — run_op file.read result.
    # Retained so unit tests that inject `data._input_raw` directly still work.
    # The result shape is:
    # {"status": "ok", "content": "<JSON of swe_bench_input artifact>", ...}
    if not isinstance(raw, str) or not raw:
        input_raw = inner_data.get("_input_raw") if isinstance(inner_data, dict) else None
    else:
        input_raw = None
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
