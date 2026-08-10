#!/usr/bin/env python3
"""#4077 — enumerate every test file that fails or hangs when run in
ISOLATION, even though the full suite (CI, ``-n auto``) is green.

Motivation (lead-coder, #4077): "main で壊れている" (main is broken) would
turn CI red and someone fixes it. "単体実行だと落ちる" (fails only when run
alone) leaves CI green while the dependency grows — invisible until someone
touches that one file and pays the isolation cost by hand, which #3879's own
migration work hit repeatedly (tui-coder root-caused 2 instances by hand,
each requiring a separate detached-worktree comparison against origin/main).
#3879 already replaced "how many flat files are left" with a machine-derived
count instead of asking a person — this script is the same move for "how
many isolation-dependent test files are there."

## Method

For each collected test file under ``tests/`` (matching pytest's own
collection glob, ``test_*.py``, excluding ``tests/scaffold/`` — a
migration-lifespan bucket with its own ``triggered_by``/``removed_by``
churn, not a stable population), run it ALONE with the SAME flags CI uses
for the full suite (``.github/workflows/test.yml``'s ``-n auto
--timeout=120``, read directly, not hand-copied) and record: exit code,
whether it hit the per-file wall-clock timeout (a hang), and — only on
failure — a short excerpt of the failure output for triage.

This is a REAL subprocess per file, no mocking of pytest itself — the same
reasoning ``test_check_migration_diff_shape_3879.py``'s own docstring gives
for using real git repos: faking the thing under test would test nothing
real.

## What this does NOT determine (measured absence, not silence)

- WHETHER a given isolated failure is order-dependent on a SPECIFIC other
  file, or a genuine standalone bug — this script only measures "fails
  alone", not "why". See #4077's own proposed step ② for the follow-up.

## ``--jobs`` — a deliberate, measured departure from CI's exact flags

``ci_pytest_flags()`` still exists and is used verbatim by default for a
SINGLE file (``--only``) — the CI-faithful comparison #4077's step ① asked
for. For the full-tree scan (step ③, the "本命"/main deliverable), running
CI's own ``-n auto`` PER FILE serially is the wrong shape: measured
directly (not assumed) against a real, trivially-passing 27-test file —
``-n auto`` costs 10.4s (xdist worker fork+import overhead dominates a
file with almost nothing to parallelize), the SAME file with no ``-n``
flag at all costs 0.14s, a ~75x difference. At ~1175 files that overhead
alone is the difference between a background job finishing in this
session and one that does not. ``--jobs N`` runs N files CONCURRENTLY
(via ``ThreadPoolExecutor`` — each ``subprocess.run`` call releases the
GIL while the child process runs, so this is real OS-level parallelism,
not faked), each WITHOUT ``-n auto`` (a single-process pytest per file),
trading "per-file worker parallelism" for "cross-file parallelism" — the
question this scan answers ("does this file need something ANOTHER
file's run left behind") does not depend on whether the file's OWN run
used xdist workers internally, only on whether it was run alone; verified
directly on a real prior isolation failure (`test_3310_n2_reset_hydrate.py`)
that the same 9-test failure reproduces identically with and without
``-n auto`` before relying on this substitution for the full scan.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_TESTS_DIR = _ROOT / "tests"
_WORKFLOW_PATH = _ROOT / ".github" / "workflows" / "test.yml"
_EXCLUDED_DIR_NAMES = frozenset({"scaffold"})
_DEFAULT_PER_FILE_TIMEOUT = 150  # CI's own --timeout=120 + startup/collection headroom


def ci_pytest_flags(workflow_path: Path = _WORKFLOW_PATH) -> "list[str]":
    """The exact ``pytest`` flags CI's own full-suite step uses, parsed from
    the workflow file itself rather than hand-copied — so this script goes
    stale LOUDLY (a parse failure) rather than silently drifting from CI's
    real invocation the next time someone edits the workflow.

    Expected line shape (see the workflow's own comment for why the
    ``timeout`` wrapper exists): ``timeout 12m python -m pytest -q -n auto
    --timeout=120 -rs``.
    """
    text = workflow_path.read_text(encoding="utf-8")
    match = re.search(r"python -m pytest (.+)$", text, re.MULTILINE)
    if match is None:
        raise ValueError(
            f"could not find a 'python -m pytest ...' line in {workflow_path} "
            "— CI's invocation may have changed shape; update this parser "
            "rather than hand-copying flags."
        )
    return match.group(1).split()


def collected_test_files(
    tests_dir: Path = _TESTS_DIR, excluded: "frozenset[str]" = _EXCLUDED_DIR_NAMES,
) -> "list[Path]":
    """Every ``test_*.py`` file under *tests_dir*, sorted for a stable scan
    order, excluding any file with an excluded directory name anywhere in
    its path (see module docstring for why ``scaffold`` specifically)."""
    out = []
    for path in tests_dir.rglob("test_*.py"):
        if excluded & set(path.relative_to(tests_dir).parts[:-1]):
            continue
        out.append(path)
    return sorted(out)


def run_one_file(
    path: Path, flags: "list[str]", root: Path = _ROOT,
    per_file_timeout: int = _DEFAULT_PER_FILE_TIMEOUT,
) -> dict:
    """Run *path* alone with CI's own *flags*, real subprocess. Returns a
    dict with ``path`` (repo-relative), ``outcome`` ("passed" / "failed" /
    "hung" / "collection_error"), ``duration_s``, and — for anything but a
    clean pass — a short ``excerpt`` (last ~40 lines of combined
    stdout+stderr) for triage."""
    rel = path.relative_to(root).as_posix()
    cmd = [sys.executable, "-m", "pytest", *flags, rel]
    start = time.monotonic()
    try:
        proc = subprocess.run(
            cmd, cwd=root, capture_output=True, text=True, timeout=per_file_timeout,
        )
        duration = time.monotonic() - start
        combined = proc.stdout + proc.stderr
        if proc.returncode == 0:
            return {"path": rel, "outcome": "passed", "duration_s": round(duration, 1)}
        outcome = "collection_error" if "ERROR" in combined and "collected 0 items" in combined else "failed"
        return {
            "path": rel,
            "outcome": outcome,
            "duration_s": round(duration, 1),
            "excerpt": "\n".join(combined.splitlines()[-40:]),
        }
    except subprocess.TimeoutExpired as exc:
        duration = time.monotonic() - start
        # `text=True` on the parent Popen call decodes stdout/stderr as str
        # in the normal case, but TimeoutExpired's own type stub declares
        # them as `bytes | str | None` regardless — decode defensively so
        # this doesn't crash on whichever type actually comes back.
        out = exc.stdout or ""
        err = exc.stderr or ""
        combined = (
            (out.decode("utf-8", errors="replace") if isinstance(out, bytes) else out)
            + (err.decode("utf-8", errors="replace") if isinstance(err, bytes) else err)
        )
        return {
            "path": rel,
            "outcome": "hung",
            "duration_s": round(duration, 1),
            "excerpt": "\n".join(combined.splitlines()[-40:]) if combined else "(no output before timeout)",
        }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument(
        "--out", type=Path, default=None,
        help=(
            "write results here as JSON LINES (one result object per line, "
            "flushed after each file) — inspectable mid-run for a long "
            "background scan, not just a single JSON array written at the "
            "end (default: print a summary only)"
        ),
    )
    parser.add_argument(
        "--only", type=str, default=None,
        help="substring filter — only run files whose repo-relative path contains this",
    )
    parser.add_argument(
        "--per-file-timeout", type=int, default=_DEFAULT_PER_FILE_TIMEOUT,
        help=f"wall-clock seconds before a file is declared 'hung' (default {_DEFAULT_PER_FILE_TIMEOUT})",
    )
    parser.add_argument(
        "--jobs", type=int, default=1,
        help=(
            "run this many files CONCURRENTLY (see module docstring's "
            "'--jobs' section) — each WITHOUT -n auto when > 1, since "
            "per-file xdist worker spin-up dominates cost at scale. "
            "--jobs 1 (default) keeps CI's exact flags, unchanged."
        ),
    )
    return parser


def main(argv: "list[str] | None" = None) -> int:
    args = build_parser().parse_args(argv)
    flags = ci_pytest_flags()
    if args.jobs > 1:
        # See module docstring's "--jobs" section for the measured
        # rationale — drop -n/auto specifically, keep every other flag
        # (e.g. --timeout, -rs) exactly as CI declares them.
        flags = [f for f in flags if f not in ("-n", "auto")]
    files = collected_test_files()
    if args.only:
        files = [f for f in files if args.only in f.relative_to(_ROOT).as_posix()]

    print(
        f"scanning {len(files)} file(s), jobs={args.jobs}, flags: {' '.join(flags)}",
        file=sys.stderr,
    )
    out_handle = args.out.open("w", encoding="utf-8") if args.out else None
    write_lock = threading.Lock()
    results: "list[dict]" = []
    done = 0

    def _record(result: dict) -> None:
        nonlocal done
        with write_lock:
            results.append(result)
            done += 1
            if out_handle is not None:
                out_handle.write(json.dumps(result) + "\n")
                out_handle.flush()
            marker = {"passed": ".", "failed": "F", "hung": "H", "collection_error": "E"}[result["outcome"]]
            print(marker, end="", flush=True, file=sys.stderr)
            if done % 80 == 0:
                print(f"  {done}/{len(files)}", file=sys.stderr)

    try:
        if args.jobs > 1:
            with ThreadPoolExecutor(max_workers=args.jobs) as pool:
                futures = [
                    pool.submit(run_one_file, path, flags, per_file_timeout=args.per_file_timeout)
                    for path in files
                ]
                for future in as_completed(futures):
                    _record(future.result())
        else:
            for path in files:
                _record(run_one_file(path, flags, per_file_timeout=args.per_file_timeout))
    finally:
        if out_handle is not None:
            out_handle.close()
    print(file=sys.stderr)

    # --jobs > 1 completes out of file order (as_completed) — sort for a
    # stable, reproducible report regardless of scheduling.
    failing = sorted(
        (r for r in results if r["outcome"] != "passed"), key=lambda r: r["path"],
    )
    if args.out:
        print(f"full report (JSON lines): {args.out}", file=sys.stderr)

    print(f"\n{len(files)} file(s) scanned, {len(failing)} isolation-dependent:")
    for r in failing:
        print(f"  [{r['outcome']}] {r['path']} ({r['duration_s']}s)")
    return 1 if failing else 0


if __name__ == "__main__":
    raise SystemExit(main())
