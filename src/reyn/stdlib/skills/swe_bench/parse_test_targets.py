"""FP-0008 C6 v2 — parse test_patch targets for sandboxed-exec revert.

Safe-mode python step: parses ``+++ b/<path>`` diff header lines from the
sanitized ``data.test_patch`` and returns a list of
``["git", "checkout", "HEAD", "--", <path>]`` argv lists — one per unique
target, excluding ``/dev/null`` and blank paths.

## Why pure string parsing (no subprocess)

v1 (PR #1098) placed ``import subprocess`` in a ``mode: safe`` python step.
The safe-mode sandbox rejects subprocess at AST parse time
(``SafeModeViolation``) — the step aborted before any code ran, and the
verify preprocessor errored on every instance.

v2 separates concerns:
- This module (mode: safe): pure string → argv-list transform (re + json
  only, both in PURE_STDLIB_ALLOWLIST).
- Iterate + sandboxed_exec run_op (OS-owned): runs each git checkout argv via
  the op_runtime sandboxed_exec handler, which anchors the subprocess to
  ``cwd=workspace.base_dir`` (FP-0008 PR-I, restored for sandboxed_exec in the
  cwd-anchor PR) — the SWE-bench repo root, correct for concurrent benchmarks.
  #1115 Stage 2 migrated this off the deprecated ``shell`` op so repo exec
  routes through the run's EnvironmentBackend (host or container).

## Why args_from {argv: _iter.item} works

``_materialize_op`` in ``preprocessor_executor.py`` resolves dot-paths from
the ``iter_artifact`` dict, which includes ``_iter.item`` injected by
``_apply_iterate``. ``SandboxedExecIROp`` has ``argv: list[str]`` as a settable
field, so ``model_copy(update={"argv": item})`` replaces the placeholder argv.
Each iteration builds a fresh ``SandboxedExecIROp`` with the resolved argv list.

## Input shape

Called with the full runtime artifact dict. Source priority mirrors
``sanitize_test_patch.py`` (P5 deterministic entry-input passthrough):

0. ``_skill_input.data.test_patch`` — the OS-injected original entry artifact
   (#1115 Stage 0), placed at the top-level ``_skill_input`` binding before the
   preprocessor runs.  Deterministic, never LLM-mutated.
1. ``data._input_raw.content`` — JSON of the original ``swe_bench_input``
   artifact from a ``run_op: file.read`` step (retained for back-compat).
2. ``data.test_patch`` — set by the sanitize_test_patch step that precedes
   this one in the preprocessor chain.
3. Top-level ``test_patch`` — flat unit-test direct-call shape.

## Output schema

``array of array-of-strings`` — zero or more
``["git", "checkout", "HEAD", "--", <path>]`` argv lists.  Returns ``[]`` on
absent/empty test_patch (graceful no-op).
"""
from __future__ import annotations

import json
import re
from typing import Any, Mapping


def _extract_test_patch(data: Mapping[str, Any]) -> str:
    """Extract the test_patch string from the artifact, following priority chain.

    Priority:
    0. _skill_input.data.test_patch (OS-injected entry artifact, #1115 Stage 0)
    1. data._input_raw.content (workspace JSON via file.read run_op — back-compat)
    2. data.test_patch (inner data dict — set by sanitize_test_patch)
    3. top-level test_patch (flat unit-test shape)

    Returns empty string when all sources are absent/non-string.
    """
    inner: Any = data.get("data") or {}
    test_patch: Any = None

    # Priority 0 (#1115 Stage 0): OS-injected entry input. The skill's original
    # entry artifact is placed at the top-level `_skill_input` binding before
    # the preprocessor runs — deterministic P5 source, never LLM-mutated.
    skill_input = data.get("_skill_input")
    if isinstance(skill_input, dict):
        si_data = skill_input.get("data")
        if isinstance(si_data, dict):
            test_patch = si_data.get("test_patch")

    # Priority 1 (back-compat): workspace passthrough — file.read run_op result.
    if not isinstance(test_patch, str) or not test_patch:
        input_raw = inner.get("_input_raw") if isinstance(inner, dict) else None
        if isinstance(input_raw, dict):
            content = input_raw.get("content")
            if isinstance(content, str) and content.strip():
                try:
                    parsed = json.loads(content)
                    test_patch = (parsed.get("data") or {}).get("test_patch")
                except (json.JSONDecodeError, AttributeError):
                    pass

    # Priority 2: inner data.test_patch
    if not isinstance(test_patch, str) or not test_patch:
        if isinstance(inner, dict):
            test_patch = inner.get("test_patch")

    # Priority 3: top-level flat shape
    if not isinstance(test_patch, str) or not test_patch:
        test_patch = data.get("test_patch")  # type: ignore[assignment]

    if not isinstance(test_patch, str):
        return ""
    return test_patch


def _parse_paths(test_patch: str) -> list[str]:
    """Return deduplicated repo-relative paths from ``+++ b/<path>`` headers.

    Excludes ``/dev/null`` and blank paths. Preserves insertion order.
    """
    seen: set[str] = set()
    paths: list[str] = []
    for line in test_patch.splitlines():
        m = re.match(r"^\+\+\+ b?/?(.*)", line)
        if not m:
            continue
        path = m.group(1).strip()
        if not path or path == "/dev/null" or path.startswith("dev/null"):
            continue
        if path not in seen:
            seen.add(path)
            paths.append(path)
    return paths


def parse_test_targets(data: Mapping[str, Any]) -> list[list[str]]:
    """Parse test_patch targets and return git checkout argv lists.

    Returns a list of argv lists of the form::

        [["git", "checkout", "HEAD", "--", "tests/test_x.py"], ...]

    One entry per unique ``+++ b/<path>`` target in the sanitized test_patch,
    excluding ``/dev/null``.  Returns ``[]`` when test_patch is absent or empty.

    Each entry is an argv list (not a shell command string) because the verify /
    report preprocessors iterate it into ``sandboxed_exec`` ops, whose ``argv``
    field is a ``list[str]`` executed directly (no shell). ``args_from:
    {argv: _iter.item}`` binds one argv list per iteration.

    This is a pure string transform — no subprocess, no filesystem access, no
    environment reads.  It is safe to call in ``mode: safe`` (uses only ``re``
    and ``json`` from PURE_STDLIB_ALLOWLIST).
    """
    test_patch = _extract_test_patch(data)
    if not test_patch.strip():
        return []
    paths = _parse_paths(test_patch)
    return [["git", "checkout", "HEAD", "--", path] for path in paths]
