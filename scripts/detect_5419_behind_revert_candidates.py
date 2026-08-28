#!/usr/bin/env python3
"""#5419 — report (never block) files a branch silently reverts by being
BEHIND main.

## Why this exists

Tonight (2026-08-28), 4 PRs (#5415, #5417, #5418, #5414) each went
`mergeStateStatus=BEHIND` at some point and, in that state, their diff
against `main` showed a file rewritten back to an OLDER line the branch
itself never touched — a real revert-in-waiting. All 4 were gate-GREEN:
a revert is a same-syntax old-value rewrite, so syntax/build/test all
pass and `check-doc-drift` cannot fire (the doc is not out of sync with
the mechanism — it is back in sync with the OLD one). This is
architect's finding (issue #5419): **not a class `test` can catch — a
class the *diff shape* catches.**

## The discriminant (architect, issue #5419 §2) — reads no intent

    git diff <base> <head> --name-only          (two-dot: base vs head)
  MINUS
    git diff <base>...<head> --name-only        (three-dot: base vs
                                                   merge-base(base,head))

Two-dot is "everything different between base and head, however it got
there". Three-dot is "what head's own history actually changed since it
diverged from base". A file in two-dot but NOT in three-dot is a file
the branch never touched, yet is different from base anyway — main
moved that file after the branch's fork point, and the branch has not
caught up. Merging it as-is discards main's own advance on that file.

## Report, not block (architect §3 — explicit ruling)

`BEHIND` is routine and mostly harmless (GitHub's merge machinery
usually resolves it correctly). Making this a blocking gate would break
on the *majority* of ordinary, harmless BEHIND states — the architect's
own "do not gate on a check without a low false-positive rate" rule.
This script and its CI wrapper are diagnostic-only: they print the
candidate file list for a human to read, and never fail the job. A
human recognizing "that's a file I never touched" is the actual backstop
(architect's own finding: all 4 of tonight's incidents were,
individually, things a person could have caught by looking — but
routinely did not, including architect themselves once, per their own
disclosure on the issue).

## Non-vacuity — this script's own positive control

A gate whose comparison is wired wrong (compares the same ref to
itself, or a base that was never fetched) reads a permanent, silent 0 —
which looks identical to "genuinely clean". Per lead-coder's explicit
acceptance condition on #5419, this script's own tests exercise it
against a REAL commit pair from tonight's incidents, not a synthetic
fixture: `a965d8e73` (main, post-#5413) vs `493bf70cd8` (an actual
#5415 PR head, captured live while it was BEHIND) — verified by hand
(2026-08-28) to produce exactly
`docs/deep-dives/contributing/verification-hazards.md`, the real file
#5413 touched that the branch had not yet caught up on. See
`tests/scripts/test_detect_5419_behind_revert_candidates.py`.

## Scope

Pure diffing logic (`behind_revert_candidates`, `format_report`) takes
already-computed two-dot/three-dot file lists — no I/O, fully
unit-testable offline (mirrors `detect_4986_teardown_hang.py`'s
network-free split). The thin `_git_diff_name_only` / `_post_pr_comment`
wrappers below are the separately-swappable network/subprocess layer.

Usage:
    python scripts/detect_5419_behind_revert_candidates.py --base origin/main --head HEAD
    python scripts/detect_5419_behind_revert_candidates.py --base origin/main --head HEAD --pr 123 --post
"""
from __future__ import annotations

import argparse
import subprocess
import sys


def behind_revert_candidates(
    two_dot_files: "list[str]", three_dot_files: "list[str]",
) -> "list[str]":
    """Files in *two_dot_files* but not *three_dot_files* — sorted, deduped.

    Pure set difference. See the module docstring for what "two-dot" /
    "three-dot" mean and why the difference names a revert candidate."""
    return sorted(set(two_dot_files) - set(three_dot_files))


def format_report(branch_label: str, candidates: "list[str]") -> str:
    """The report text — always names every file, never just a count
    (lead-coder's #5419 acceptance point ①, citing #4357's own measured
    finding: a bare count moved nobody to act)."""
    if not candidates:
        return f"OK — no behind-revert candidates for {branch_label}."
    lines = "\n".join(f"  - `{f}`" for f in candidates)
    return (
        f"REPORT (non-blocking) — {branch_label} is BEHIND `main` on the "
        f"following file(s) it never itself touched. Merging as-is would "
        f"silently revert main's own lines there:\n{lines}\n\n"
        "This is a report, not a block (#5419 §3) — most BEHIND states "
        "are harmless and GitHub's merge resolves them correctly. If any "
        "of the above is a file you did not intend to touch, run "
        "`update-branch` (or rebase) before merging."
    )


# ---------------------------------------------------------------------------
# git/gh wrapper (thin — kept separate from the pure logic above)
# ---------------------------------------------------------------------------


def _git_diff_name_only(base: str, head: str, *, dots: str) -> "list[str]":
    """*dots* is literally ``".."`` or ``"..."`` — passed straight into the
    git diff spec (``git diff base..head`` / ``git diff base...head``)."""
    spec = f"{base}{dots}{head}"
    result = subprocess.run(
        ["git", "diff", spec, "--name-only"],
        capture_output=True, text=True, check=True,
    )
    return [line for line in result.stdout.splitlines() if line]


def _resolve_pr_refs(pr: str) -> "tuple[str, str]":
    """(base, head) SHAs for an open PR — mirrors `check_doc_drift.py`'s
    own `gh pr view --json baseRefOid,headRefOid` resolution, so both
    scripts read the same fields the same way."""
    import json

    result = subprocess.run(
        ["gh", "pr", "view", str(pr), "--json", "baseRefOid,headRefOid"],
        capture_output=True, text=True, check=True,
    )
    data = json.loads(result.stdout)
    return data["baseRefOid"], data["headRefOid"]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Report (never block) files a branch silently reverts by being BEHIND main.",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--pr", metavar="N", help="PR number — base/head resolved via `gh pr view`.")
    group.add_argument(
        "--base", metavar="REF",
        help="Explicit base ref (with --head) — offline/direct invocation, no `gh` needed.",
    )
    parser.add_argument("--head", metavar="REF", help="Explicit head ref — required together with --base.")
    return parser


def main(argv: "list[str] | None" = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.base and not args.head:
        parser.error("--base requires --head")

    try:
        if args.pr:
            base, head = _resolve_pr_refs(args.pr)
            branch_label = f"PR #{args.pr}"
        else:
            base, head = args.base, args.head
            branch_label = args.head
        two_dot = _git_diff_name_only(base, head, dots="..")
        three_dot = _git_diff_name_only(base, head, dots="...")
    except subprocess.CalledProcessError as exc:
        print(f"git/gh command failed: {exc.stderr}", file=sys.stderr)
        return 2

    candidates = behind_revert_candidates(two_dot, three_dot)
    report = format_report(branch_label, candidates)
    print(report)

    # Signal-only exit code (1 = candidates found) — the CI wrapper never
    # treats this as job failure (#5419 §3: report, not block).
    return 1 if candidates else 0


if __name__ == "__main__":
    raise SystemExit(main())
