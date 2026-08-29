#!/usr/bin/env python3
"""#5478 ⑤ — report (never block) the files a PR's own merge would DELETE.

## The gap this closes

Before this script, "does this merge delete any file" was checked by
exactly one person, by hand, on every PR (lead-coder, #5478's own
filing): `#5474` was squash-merged on the strength of an all-green CI
run, and lead-coder's own deletion/identifier checks ran only
AFTERWARD, out of band — this time both came back clean, but nothing
would have stopped either from landing unseen on a night lead-coder
missed it (lead-coder, verbatim: **無事だったのは運です**). Charter
band question 1 — "who stops this if it repeats" — had exactly one
answer: lead-coder personally, every single time.

## What this measures

    git diff <base>...<head> --diff-filter=D --name-only

Three-dot (base vs. merge-base(base, head)) — the same discriminant
`detect_5419_behind_files.py` uses for its own two-dot/three-dot split,
and for the identical reason: a real merge (or squash) only ever
applies what the BRANCH itself changed since it diverged from base, so
three-dot is the set of paths this PR's own diff actually touches.
`--diff-filter=D` narrows that to deletions. This is exactly the
one-command form lead-coder specified in #5478's own body.

## Report-only, deliberately (lead-coder, #5478 body, verbatim)

> 削除が在ること自体は悪ではありません。見えることが要点

A deletion is not itself a defect — refactors, dead-code removal, and
retired scaffolding all delete files on purpose, routinely, in this
repo. Blocking on "any deletion" would fail the ordinary case far more
often than the rare unintended one. This script never fails the job —
it prints the list (never just a count — the same #5419 §① rationale
`detect_5419_behind_files.format_report` already established: a bare
count moved nobody to act) so a reviewer sees it without having to run
lead-coder's own manual command first.

## Scope (says only what it checked, not what it guarantees)

This reports FILE-level deletions from the PR's own three-dot diff. It
says nothing about whether a deleted file's identifiers still linger in
`docs/` — that is a SEPARATE, narrower question `check_doc_drift.py`
already asks (see that script's own module docstring for its own scope
disclaimer, tightened alongside this file in the same PR, #5478 ⑥).
Two different needles, two different scripts, on purpose — folding them
together would blur which one actually fired.

Usage:
    python scripts/detect_5478_deleted_files.py --pr 123
    python scripts/detect_5478_deleted_files.py --base origin/main --head HEAD
"""
from __future__ import annotations

import argparse
import subprocess
import sys


def deleted_files(three_dot_diff_filter_d: "list[str]") -> "list[str]":
    """Sorted, deduped — pure pass-through of an already-`--diff-filter=D`
    file list. Kept as its own function (rather than inlined in `main`)
    so the shape mirrors `detect_5419_behind_files.behind_files` — a
    thin, offline-testable seam between "what git reported" and "what
    this script does with it"."""
    return sorted(set(f for f in three_dot_diff_filter_d if f))


def format_report(branch_label: str, deleted: "list[str]") -> str:
    """Report text — always names every file, never just a count (same
    #5419 §① rationale `detect_5419_behind_files.format_report` already
    established). Deliberately neutral wording: a deletion here is a
    fact to see, not an accusation — see module docstring, "Report-only"."""
    if not deleted:
        return f"OK — {branch_label} deletes no file (this PR's own three-dot diff)."
    lines = "\n".join(f"  - `{f}`" for f in deleted)
    return (
        f"REPORT (non-blocking) — {branch_label} deletes the following "
        f"file(s):\n{lines}\n\n"
        "A deletion is not itself a problem — this is visibility, not a "
        "verdict (#5478). It says nothing about whether an identifier "
        "any of these files defined still lingers in docs/ — see "
        "check_doc_drift.py for that separate, narrower question."
    )


# ---------------------------------------------------------------------------
# git/gh wrapper (thin — kept separate from the pure logic above)
# ---------------------------------------------------------------------------


def _git_diff_filter_d_name_only(base: str, head: str) -> "list[str]":
    result = subprocess.run(
        ["git", "diff", f"{base}...{head}", "--diff-filter=D", "--name-only"],
        capture_output=True, text=True, check=True,
    )
    return [line for line in result.stdout.splitlines() if line]


def _resolve_pr_refs(pr: str) -> "tuple[str, str]":
    """(base, head) SHAs for an open PR — mirrors
    `detect_5419_behind_files._resolve_pr_refs`/`check_doc_drift.py`'s
    own `gh pr view --json baseRefOid,headRefOid` resolution, so all
    three scripts read the same fields the same way."""
    import json

    result = subprocess.run(
        ["gh", "pr", "view", str(pr), "--json", "baseRefOid,headRefOid"],
        capture_output=True, text=True, check=True,
    )
    data = json.loads(result.stdout)
    return data["baseRefOid"], data["headRefOid"]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Report (never block) files a PR's own merge would DELETE (#5478 ⑤).",
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
        deleted_raw = _git_diff_filter_d_name_only(base, head)
    except subprocess.CalledProcessError as exc:
        print(f"git/gh command failed: {exc.stderr}", file=sys.stderr)
        return 2

    deleted = deleted_files(deleted_raw)
    report = format_report(branch_label, deleted)
    print(report)

    # Signal-only exit code (1 = deletions found) — the CI wrapper never
    # treats this as job failure (#5478: report, not block).
    return 1 if deleted else 0


if __name__ == "__main__":
    raise SystemExit(main())
