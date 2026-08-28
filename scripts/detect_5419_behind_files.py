#!/usr/bin/env python3
"""#5419 — report (never block) files a branch is BEHIND main on.

## What this measures (and, explicitly, what it does NOT)

    git diff <base> <head> --name-only          (two-dot: base vs head)
  MINUS
    git diff <base>...<head> --name-only        (three-dot: base vs
                                                   merge-base(base,head))

Two-dot answers "what would this file look like if the branch fully
*replaced* base". Three-dot answers "what has the branch's own history
actually changed since it diverged from base" — and merging only ever
applies the THREE-DOT diff (a real three-way merge takes main's version
for any file the branch itself never touched; a squash merge applies
only the branch's own diff). A file in two-dot but not three-dot is one
the branch never touched, yet differs from base anyway — main moved
that file after the branch's fork point and the branch has not caught
up. **This is a `BEHIND` fact, not a revert-on-merge fact.**

## ⚠️ Corrected framing (architect, 2026-08-28, PR #5420 review)

An earlier revision of this script called this set "revert candidates"
and its CI wrapper "BEHIND-revert candidate report". That framing was
WRONG, and it cost real time: lead-coder read this exact two-dot set on
4 real PRs that night (#5414/#5415/#5417/#5418) as "this branch reverts
main on merge", raised blocking review comments, and drove 14 combined
`update-branch` cycles chasing what turned out to be normal, harmless
`BEHIND` states. Direct evidence that falsified it: `git show
--name-only --diff-filter=D --format= <merge-commit>` for #5415's and
#5417's own merge commits shows **zero files deleted** by either merge,
and `git merge-tree --write-tree origin/main <branch>` for #5418 shows
main's OWN current text surviving in the merged tree — none of the 4
"reverts" would have actually happened. The mistake: reading the
branch's own (pre-merge) tree content as if it were the merge's output.
It never touched the file, so of course its own tree still shows the
old line — that is not what landing the merge produces.

∴ This set is still real and worth surfacing (a reviewer opening the
file on the branch side reads stale content, which can itself be
confusing) — but the correct question it answers is **"is this branch
behind main on file X", never "will merging this branch revert file
X"**. If you want an actual revert check, that's a different pair of
commands, deliberately NOT implemented here (scope decision, not an
oversight):

    git diff <base>...<head> --diff-filter=D --name-only   # files the
                                                             # branch ITSELF deletes
    git merge-tree --write-tree <base> <head>               # read the
                                                             # real merge result directly

## Report, not block (architect §3 — explicit ruling, unaffected by the
## framing correction above)

`BEHIND` is routine and mostly harmless (GitHub's merge machinery
usually resolves it correctly). Making this a blocking gate would break
on the *majority* of ordinary, harmless BEHIND states. This script and
its CI wrapper are diagnostic-only: they print the file list for a
human to read, and never fail the job.

## Non-vacuity — this script's own positive control

A gate whose comparison is wired wrong (compares the same ref to
itself, or a base that was never fetched) reads a permanent, silent 0 —
which looks identical to "genuinely clean". This script's tests build a
real, disposable git repo under `tmp_path` (base commit → branch off →
base advances a file the branch never touches) so the two-dot/three-dot
divergence is CONSTRUCTED fresh on every run, not read from a fixed
historical SHA pair. An earlier revision of this test suite instead
replayed two real commit SHAs from the night this was built
(`a965d8e73` / `493bf70cd8...`) — correct in the author's own shallow-
free local clone, but SKIPPED unconditionally in CI (the `pytest` job's
`actions/checkout` is shallow, `fetch-depth` unset ⇒ depth 1 ⇒ those
SHAs are simply absent) and would have gone stale once the source
branch was deleted post-merge regardless. See
`tests/scripts/test_detect_5419_behind_files.py`.

## Scope

Pure diffing logic (`behind_files`, `format_report`) takes
already-computed two-dot/three-dot file lists — no I/O, fully
unit-testable offline (mirrors `detect_4986_teardown_hang.py`'s
network-free split). The thin `_git_diff_name_only` / `_resolve_pr_refs`
wrappers below are the separately-swappable network/subprocess layer.

Usage:
    python scripts/detect_5419_behind_files.py --base origin/main --head HEAD
    python scripts/detect_5419_behind_files.py --pr 123
"""
from __future__ import annotations

import argparse
import subprocess
import sys


def behind_files(
    two_dot_files: "list[str]", three_dot_files: "list[str]",
) -> "list[str]":
    """Files in *two_dot_files* but not *three_dot_files* — sorted, deduped.

    Pure set difference. See the module docstring for what "two-dot" /
    "three-dot" mean and, importantly, what this difference does and
    does not tell you about a future merge's result."""
    return sorted(set(two_dot_files) - set(three_dot_files))


def format_report(branch_label: str, behind: "list[str]") -> str:
    """The report text — always names every file, never just a count
    (lead-coder's #5419 acceptance point ①, citing #4357's own measured
    finding: a bare count moved nobody to act). Wording is deliberately
    revert-free: see the module docstring's "Corrected framing" section
    for why an earlier revision's "revert candidate" language was
    itself a false claim."""
    if not behind:
        return f"OK — {branch_label} is not behind main on any file it hasn't itself touched."
    lines = "\n".join(f"  - `{f}`" for f in behind)
    return (
        f"REPORT (non-blocking) — {branch_label} is behind `main` on the "
        f"following file(s) it has not itself touched:\n{lines}\n\n"
        "Merging will NOT revert these — a real merge (or squash) only "
        "ever applies the branch's own changes, so main's current "
        "content survives untouched. This is a report, not a block "
        "(#5419 §3): reading the file ON THE BRANCH SIDE right now "
        "shows stale content, which is the only thing worth knowing "
        "here."
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
        description="Report (never block) files a branch is BEHIND main on.",
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

    behind = behind_files(two_dot, three_dot)
    report = format_report(branch_label, behind)
    print(report)

    # Signal-only exit code (1 = behind-files found) — the CI wrapper
    # never treats this as job failure (#5419 §3: report, not block).
    return 1 if behind else 0


if __name__ == "__main__":
    raise SystemExit(main())
