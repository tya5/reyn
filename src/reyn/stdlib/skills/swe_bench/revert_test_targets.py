"""Deterministic test-target revert for swe_bench verify phase.

FP-0008 C6 — verify preprocessor reverts test_patch-target files to HEAD
before the LLM applies the test_patch. Eliminates the apply×verify loop
caused by the LLM editing test files that the test_patch also touches.

## Problem (C6)

The apply phase may edit test files (there is no structural prohibition).
The verify phase applies the test_patch (``git apply test_patch``), which
also modifies those test files.  When the apply LLM has already written to
a file the test_patch targets, ``git apply`` fails with
``error: patch failed: <path>: does not match index`` — the patch context
no longer matches the working tree.  The verify phase then transitions back
to apply, which re-edits, re-triggers the same failure, and the cycle
repeats until ``loop_limit_exceeded``.

## Fix (deterministic, in the preprocessor)

This module is the deterministic corrective step. It runs as a
``type: python`` preprocessor step in verify BEFORE the LLM call,
after the test_patch sanitizer step (which guarantees
``data.test_patch`` is a clean string).

Steps:
1. Parse ``+++ b/<path>`` diff header lines from ``data.test_patch``.
   Excludes ``/dev/null`` (= new-file targets that cannot be reverted).
2. For each target path, run ``git checkout HEAD -- <path>`` in the
   repo's working directory (= ``data._repo_dir.stdout``, captured by
   a preceding ``run_op: shell: cmd: pwd`` step that runs with
   ``cwd=workspace.base_dir``).
3. Return a summary dict with the reverted paths and any per-path errors.
   The ``into:`` target (``data._revert_result``) is informational only.

## Graceful no-op (on_error: empty discipline)

The preprocessor step has ``on_error: empty`` as its fallback. The
function MUST NOT raise on absent/non-string inputs, missing repo_dir,
or empty test_patch — it returns ``{"reverted": [], "errors": []}`` so
the preprocessor treats it as an enrichment step, not a hard gate.

## Repo-dir determination

The repo working directory is captured by a preceding ``run_op`` shell
step (``cmd: pwd``) whose output is stored at ``data._repo_dir``. Because
shell ops run with ``cwd=ctx.workspace.base_dir`` (FP-0008 PR-I), this
resolves to the actual SWE-bench repo checkout path, regardless of the
process-level CWD.

The python subprocess (this module) inherits the process CWD, which in
concurrent benchmark runs may be the launch directory — NOT workspace
base_dir. Threading the repo_dir via the artifact is therefore mandatory
for correctness.
"""
from __future__ import annotations

import json
import re
import subprocess
from typing import Any, Mapping


def _parse_test_patch_targets(test_patch: str) -> list[str]:
    """Return repository-relative paths targeted by ``+++ b/<path>`` lines.

    Skips ``/dev/null`` (= deleted-file targets that do not exist on disk)
    and blank paths.  Returns a deduplicated, insertion-order-preserving list.
    """
    targets: list[str] = []
    seen: set[str] = set()
    for line in test_patch.splitlines():
        # Match "+++ b/<path>" or "+++ <path>" (some diffs omit the "b/" prefix)
        m = re.match(r"^\+\+\+ b?/?(.*)", line)
        if not m:
            continue
        path = m.group(1).strip()
        # Exclude sentinel paths
        if not path or path == "/dev/null" or path.startswith("dev/null"):
            continue
        if path not in seen:
            seen.add(path)
            targets.append(path)
    return targets


def revert_test_targets(data: Mapping[str, Any]) -> dict:
    """Revert test_patch-target files to HEAD before the LLM applies the patch.

    Reads the sanitized ``data.test_patch`` and the repo dir from
    ``data._repo_dir.stdout`` (injected by the preceding ``run_op: shell: pwd``
    preprocessor step).  Runs ``git checkout HEAD -- <paths>`` for each target.

    Returns a dict::

        {
            "reverted": ["<path>", ...],   # paths successfully reverted
            "errors":   [{"path": ..., "error": ...}, ...]  # per-path errors
        }

    Always returns a dict — never raises (graceful no-op on any missing input).
    """
    empty_result: dict = {"reverted": [], "errors": []}

    # ── Extract test_patch ────────────────────────────────────────────────────
    # Priority chain mirrors sanitize_test_patch.py (P5 workspace passthrough):
    # 1. data._input_raw.content  — workspace JSON (verify & report phases)
    # 2. data.data.test_patch     — inner data dict (verify phase after sanitize)
    # 3. top-level test_patch     — flat unit-test direct-call shape
    inner_data: Any = data.get("data") or {}
    test_patch: str | None = None

    # Priority 1: workspace passthrough — same source as sanitize_test_patch
    if isinstance(inner_data, dict):
        input_raw = inner_data.get("_input_raw")
        if isinstance(input_raw, dict):
            content = input_raw.get("content")
            if isinstance(content, str) and content.strip():
                try:
                    parsed = json.loads(content)
                    test_patch = (parsed.get("data") or {}).get("test_patch")
                except (json.JSONDecodeError, AttributeError):
                    pass

    # Priority 2: inner data.test_patch (verify phase: sanitizer already wrote it)
    if not isinstance(test_patch, str) or not test_patch:
        if isinstance(inner_data, dict):
            test_patch = inner_data.get("test_patch")

    # Priority 3: top-level test_patch (flat unit-test direct-call shape)
    if not isinstance(test_patch, str) or not test_patch:
        test_patch = data.get("test_patch")  # type: ignore[assignment]

    if not isinstance(test_patch, str) or not test_patch.strip():
        # No test_patch → nothing to revert
        return empty_result

    # ── Extract repo_dir from the pwd shell run_op result ────────────────────
    repo_dir: str | None = None
    if isinstance(inner_data, dict):
        repo_dir_raw = inner_data.get("_repo_dir")
        if isinstance(repo_dir_raw, dict):
            stdout = repo_dir_raw.get("stdout", "")
            if isinstance(stdout, str):
                repo_dir = stdout.strip()

    # Flat dict fallback (unit-test direct-call shape)
    if not repo_dir:
        flat_repo_dir = data.get("_repo_dir")  # type: ignore[call-overload]
        if isinstance(flat_repo_dir, str):
            repo_dir = flat_repo_dir.strip()
        elif isinstance(flat_repo_dir, dict):
            stdout = flat_repo_dir.get("stdout", "")
            if isinstance(stdout, str):
                repo_dir = stdout.strip()

    if not repo_dir:
        # No repo_dir available — cannot revert safely (would use wrong cwd)
        return empty_result

    # ── Parse targets ─────────────────────────────────────────────────────────
    targets = _parse_test_patch_targets(test_patch)
    if not targets:
        return empty_result

    # ── Revert each target ────────────────────────────────────────────────────
    reverted: list[str] = []
    errors: list[dict] = []

    for path in targets:
        try:
            result = subprocess.run(
                ["git", "checkout", "HEAD", "--", path],
                cwd=repo_dir,
                capture_output=True,
                text=True,
                timeout=15,
            )
            if result.returncode == 0:
                reverted.append(path)
            else:
                # Non-zero exit: path may not exist in HEAD (new test file),
                # or may already be clean — treat as non-fatal.
                errors.append({
                    "path": path,
                    "error": (result.stderr or result.stdout or "").strip()[:200],
                })
        except (subprocess.TimeoutExpired, OSError, ValueError) as exc:
            errors.append({"path": path, "error": str(exc)[:200]})

    return {"reverted": reverted, "errors": errors}
