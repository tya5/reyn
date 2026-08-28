#!/usr/bin/env python3
"""#4997① — detect the #4986 teardown-hang signature in a CI run's log.

## Why this exists

`status:awaiting-recurrence` on #4986 (asyncio-runner teardown hangs in
`_cancel_all_tasks` until pytest-timeout kills it at 120s) had ZERO
detectors — owner's own finding (#4997): a "recurrence" label with no way
to notice the recurrence is a wish, not a state. This script closes that
for #4986 specifically; #4997②/③ are #4834/#4975's own separate
detectors, tracked independently.

## The signature (measured, not guessed)

Two real CI runs were cross-referenced (#4986 issue thread, lead-coder +
e2e-coder, 2026-08-21) to find what is COMMON between them, discarding
anything that appeared in only one:

    - `ERROR at teardown of <test name>` (pytest's own teardown-failure
      header)
    - `_cancel_all_tasks` (the frame inside asyncio's own
      `runners.py:close` where the hang sits — `loop.run_until_complete
      (tasks.gather(*to_cancel, ...))`, which never returns)
    - `Timeout (>120` (pytest-timeout's own kill message —
      `Failed: Timeout (>120.0s) from pytest-timeout.`)

All three, not any one alone: each individually can appear in an
UNRELATED failure (a plain teardown error with no hang; an unrelated
120s timeout with no teardown error; `_cancel_all_tasks` appears in
EVERY asyncio-runner teardown, hung or not). Co-occurrence of all three
in the SAME failure block is the signature.

## What is deliberately NOT part of the signature

`ephemeral-vanish` (`TrackedTaskSet.aclose(caller='await_quiescent'):
called reentrantly from a task ('ephemeral-vanish', ...)`) was measured
and REJECTED as a discriminator: this WARNING is a routine, frequent
message unrelated to the hang — run 31913669752 has it WITHOUT the
teardown hang, so including it would produce false positives on ordinary
runs that never hung at all.

## Scope

Reads a CI run's raw log TEXT (from `gh run view --log-failed`, `gh api
.../attempts/N/logs`, or a saved file) — the same substrate
`check_pr_closing_intent.py --fixture` uses for offline testing, kept
pure/network-free so it is fully unit-testable. The CLI wrapper below is
the thin, separately-swappable network layer.

Usage:
    python scripts/detect_4986_teardown_hang.py --run-id <id>
    python scripts/detect_4986_teardown_hang.py --log-file <path>
"""
from __future__ import annotations

import argparse
import subprocess
import sys

_SIGNATURE_MARKERS = (
    "ERROR at teardown",
    "_cancel_all_tasks",
    "Timeout (>120",
)


def has_4986_signature(log_text: str) -> bool:
    """True iff *log_text* contains all three #4986 signature markers.

    Pure, no I/O — the whole decision this script makes, isolated so it
    is directly testable against fixture log text without hitting
    GitHub. See the module docstring for what the three markers are and
    why `ephemeral-vanish` is deliberately excluded."""
    return all(marker in log_text for marker in _SIGNATURE_MARKERS)


def format_notification(run_id: str) -> str:
    """The notification text a detection must carry — per #4997's common
    requirement across all 3 detectors: the issue number to trace to, AND
    evidence-preservation instructions, IN THE SAME MESSAGE. Without the
    second half, whoever reads the alert re-runs CI to "confirm" and
    destroys the only copy of the log before anyone can read it (lead-
    coder's own incident, same night this issue was filed)."""
    return (
        f"RED #4986 — CI run {run_id} matches #4986's teardown-hang "
        "signature (ERROR at teardown + _cancel_all_tasks + "
        "Timeout (>120). Before re-running this job, preserve the "
        "evidence: `gh api /repos/tya5/reyn/actions/runs/"
        f"{run_id}/attempts/1/logs` (or `gh run view {run_id} "
        "--log-failed`) — a re-run may not reproduce the hang, and the "
        "log is the only record of this occurrence. #4986 variant B: "
        "check attempt 1's log for "
        "'TrackedTaskSet.aclose(caller=...): waiting on N tracked "
        "task(s): ...' — if present, it names exactly which task(s) "
        "were still pending when the hang started (the one thing the "
        "faulthandler-based pytest-timeout dump above cannot say, since "
        "an asyncio Task is not an OS thread)."
    )


# ---------------------------------------------------------------------------
# gh wrapper (thin — kept separate from the pure logic above)
# ---------------------------------------------------------------------------


def _fetch_run_log(run_id: str) -> str:
    result = subprocess.run(
        ["gh", "run", "view", run_id, "--log-failed"],
        capture_output=True, text=True, check=True,
    )
    return result.stdout


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Detect the #4986 teardown-hang signature in a CI run's log.",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--run-id", metavar="ID",
        help="A GitHub Actions run ID — fetched via `gh run view ID --log-failed`.",
    )
    group.add_argument(
        "--log-file", metavar="PATH",
        help="Path to a saved log file (offline / already-fetched — never re-runs CI).",
    )
    return parser


def main(argv: "list[str] | None" = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.log_file is not None:
        from pathlib import Path
        log_text = Path(args.log_file).read_text(encoding="utf-8", errors="replace")
        run_id = args.log_file
    else:
        try:
            log_text = _fetch_run_log(args.run_id)
        except subprocess.CalledProcessError as exc:
            print(f"gh run view failed: {exc.stderr}", file=sys.stderr)
            return 2
        run_id = args.run_id

    if has_4986_signature(log_text):
        print(format_notification(run_id))
        return 1

    print(f"OK — no #4986 signature found in {run_id}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
