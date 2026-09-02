#!/usr/bin/env python3
"""#5265 — detect a PR head sha whose required CI checks will NEVER be
reported, so it stays ``mergeStateStatus=BLOCKED`` silently forever.

## The failure this detects (measured, not guessed — #5265 issue thread)

2026-08-26 15:24-15:44Z: 28 `gh run list` runs, all `conclusion ==
"startup_failure"` (`jobs == 0` — GitHub itself never started the run),
across PRs #5262/#5263/#5264. Every required branch-protection context
(`pytest (Python 3.11)`, `pytest (Python 3.12)`, `ruff`, `test-tier
audit`, `docs build (strict)`) was simply ABSENT from those PRs — not
failing, not pending, never reported at all — so ``mergeStateStatus``
stayed ``BLOCKED`` with no red anywhere on the PR to notice. A 7-day
window before the incident (2026-08-15..08-22, 11,090 runs covered) had
zero occurrences — this is not a frequent/periodic failure, so no
timeout/polling cadence is appropriate here (see below).

## Architect ruling (#5265, 2026-08-30) — the design this script implements

Detection does not depend on frequency (a silent, unbounded-time failure
is worth detecting even if rare); AUTOMATIC recovery does (the only
recovery lever, PR close/reopen, drops auto-merge arming and has no
"who re-arms it" answer) — so this script only DETECTS, never acts.

The decision needs no waiting and no timeout: for a given PR head sha,
required context can be classified from STRUCTURE alone, immediately —

    - zero workflow runs recorded against this head sha  -> will NEVER
      report (the workflow never started at all)
    - every recorded run is `conclusion == "startup_failure"` with
      `jobs == 0` -> will NEVER report (GitHub accepted the trigger but
      produced nothing)
    - at least one recorded run is anything else (queued, in_progress,
      or a real conclusion) -> may still report; do NOT flag this head
      (this is what a normal, still-running CI pass looks like too, and
      the ONLY way to avoid a duration-dependent "wait N minutes" check
      — CLAUDE.md's own Ceiling rule — is to read this off run STATE,
      never off elapsed time)

## Placement (architect ruling)

NOT a GitHub Actions cron — the failure mode this detects is Actions
itself failing to start a run, so a detector living inside Actions would
go silent on exactly the days it is needed (self-referential silence).
This script is the decision logic only (pure function below, deterministic,
unit-testable, `gh`-free); it is meant to be invoked from OUTSIDE Actions
(a peer session's own sweep, or `reyn-broker`'s `github_pr_watcher.py`
plugin — a different repo, not touched here) rather than adding a new
standing process of its own.

Usage:
    python scripts/detect_5265_startup_failure_blocked_prs.py --pr 5262
    python scripts/detect_5265_startup_failure_blocked_prs.py --head-sha <sha>
    python scripts/detect_5265_startup_failure_blocked_prs.py --fixture runs.json
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from typing import Callable

_REPO = "tya5/reyn"


def is_permanently_blocked(runs: "list[dict]") -> bool:
    """True iff *runs* — every workflow run GitHub has recorded against
    ONE head sha, each ``{"conclusion": str | None, "jobs_count": int |
    None}`` — proves the required checks for this head sha will NEVER be
    reported: no runs at all, or every recorded run is a dead
    ``startup_failure``/``jobs==0`` run.

    False means at least one run is neither absent nor dead — still
    legitimately in flight (queued/in_progress) or already completed
    normally (any other conclusion) — do not flag.

    Pure, no I/O — the whole decision this script makes, isolated so it
    is directly testable against fixture data without hitting GitHub.
    accept-side note (architect, #5265): a detector that flags every
    non-empty *runs* list too would pass the "0 runs"/"all dead" cases
    for the wrong reason — this function's own deny case (a live,
    in-progress run) is what proves it actually discriminates."""
    if not runs:
        return True
    return all(_is_dead_run(r) for r in runs)


def _is_dead_run(run: dict) -> bool:
    return run.get("conclusion") == "startup_failure" and run.get("jobs_count") == 0


def format_notification(pr_number: "str | int", head_sha: str, runs: "list[dict]") -> str:
    """The notification text a detection must carry: which PR/head, and
    the recovery lever + its own known cost (#5265's own body: close/
    reopen drops auto-merge arming) — a reader must not have to re-derive
    that from the issue thread before acting."""
    if not runs:
        reason = "no workflow run was ever recorded against this head sha"
    else:
        reason = (
            f"all {len(runs)} recorded run(s) are dead "
            "(conclusion=startup_failure, jobs=0)"
        )
    return (
        f"RED #5265 — PR #{pr_number} (head {head_sha[:12]}) is "
        f"permanently BLOCKED: {reason}. Required checks will never be "
        "reported for this head. Recovery: close/reopen the PR "
        "(re-fires the pull_request trigger without moving head — but "
        "DROPS auto-merge arming, which must be re-armed after). If a "
        "run exists, `gh run rerun <id>` may work instead and does not "
        "affect auto-merge."
    )


# ---------------------------------------------------------------------------
# gh wrapper (thin — kept separate from the pure logic above)
# ---------------------------------------------------------------------------


def _gh_json(args: "list[str]") -> object:
    result = subprocess.run(["gh", *args], capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


def _fetch_head_sha_for_pr(pr_number: str) -> str:
    data = _gh_json([
        "pr", "view", pr_number, "--repo", _REPO, "--json", "headRefOid",
    ])
    return data["headRefOid"]


def _fetch_runs_for_head(
    head_sha: str, *, gh_json_fn: "Callable[[list[str]], object]" = _gh_json,
) -> "list[dict]":
    """Every workflow run GitHub has recorded for *head_sha*, normalized
    to this script's own ``{"conclusion", "jobs_count"}`` shape.

    ``gh_json_fn`` (#5665): the ONE ``gh`` call-out point, injectable —
    ``_gh_json`` for real, or a test's own Fake that returns canned
    ``gh api`` payloads and lets a regression test assert on the exact
    ARGV this function builds (proving the fix targets the QUERY itself,
    not just its interpretation — see ``tests/scripts/test_detect_5265_
    startup_failure_blocked_prs.py``'s own head_sha-outside-the-old-
    window fixture, built from PR #5640's real shape).

    #5665 (lead-coder's own real-machine finding, PR #5640): ``gh run
    list`` has no server-side head-sha filter, so an earlier revision of
    this function fetched the repo's own most-recent ``--limit 200`` runs
    and filtered client-side — a WINDOW, not a complete read. A busy repo
    day (20 PRs merged) shrank that window to ~70 minutes of coverage;
    any head older than that produced an EMPTY ``matching`` list
    indistinguishable from "GitHub never recorded a run at all" —
    :func:`is_permanently_blocked`'s own ``if not runs: return True``
    then fired on a query that never reached the real data, not on a
    genuine #5265 incident. "Increase 200" would only widen the same
    window, not remove it — the SAME false-RED shape recurs on a busier
    day or an older head.

    The REST endpoint ``GET /repos/{repo}/actions/runs?head_sha=<sha>``
    (unlike ``gh run list``) filters server-side and returns
    ``{"total_count", "workflow_runs": [...]}`` — the ``head_sha`` query
    parameter must be embedded in the URL itself, not passed via ``gh
    api -f`` (``-f``/``-F`` switch ``gh api``'s own default method from
    GET to POST, which this read-only endpoint rejects with a 404 —
    verified by direct execution, not assumed). ``--paginate`` makes the
    read provably complete regardless of how many runs exist for this
    ONE head sha (architect's own #5265 structural requirement: a "0"
    used as detection evidence must be a 0 over the FULL population, not
    a page's own emptiness) — costs nothing extra when everything already
    fits on one page, which every real head sha does in practice, but
    removes the same window-shaped bug this function existed to fix
    rather than only patching today's specific instance of it.

    ``jobs_count`` is only fetched (a second `gh api` call, per run) for
    a run whose conclusion is ``startup_failure`` — every other
    conclusion already proves the run is not dead, per
    :func:`is_permanently_blocked`'s own logic, so job count is
    irrelevant to it."""
    data = gh_json_fn([
        "api", f"repos/{_REPO}/actions/runs?head_sha={head_sha}", "--paginate",
    ])
    matching = data.get("workflow_runs", [])
    runs: "list[dict]" = []
    for r in matching:
        conclusion = r.get("conclusion") or None
        jobs_count = None
        if conclusion == "startup_failure":
            jobs = gh_json_fn([
                "api", f"repos/{_REPO}/actions/runs/{r['id']}/jobs", "--paginate",
            ])
            jobs_count = len(jobs.get("jobs", []))
        runs.append({"conclusion": conclusion, "jobs_count": jobs_count})
    return runs


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Detect a PR head sha whose required CI checks will never report (#5265).",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--pr", metavar="N", help="PR number — head sha resolved via `gh pr view`.")
    group.add_argument("--head-sha", metavar="SHA", help="A commit sha directly (no PR lookup).")
    group.add_argument(
        "--fixture", metavar="PATH",
        help="Path to a JSON file shaped like _fetch_runs_for_head's own return "
        "value (offline / already-fetched — never hits `gh`).",
    )
    return parser


def main(argv: "list[str] | None" = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    pr_label: "str | int" = "?"
    if args.fixture is not None:
        from pathlib import Path
        runs = json.loads(Path(args.fixture).read_text(encoding="utf-8"))
        head_sha = args.fixture
    else:
        try:
            if args.pr is not None:
                pr_label = args.pr
                head_sha = _fetch_head_sha_for_pr(args.pr)
            else:
                head_sha = args.head_sha
            runs = _fetch_runs_for_head(head_sha)
        except subprocess.CalledProcessError as exc:
            print(f"gh call failed: {exc.stderr}", file=sys.stderr)
            return 2

    if is_permanently_blocked(runs):
        print(format_notification(pr_label, head_sha, runs))
        return 1

    print(f"OK — head {head_sha[:12]} has at least one live/completed run, not permanently blocked.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
