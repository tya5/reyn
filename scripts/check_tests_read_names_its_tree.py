#!/usr/bin/env python3
"""Fail a ``tests/``-touching PR whose TESTS-READ note does not name the tree
it read, or names one that later commits to ``tests/`` have already left behind.

#5039. House rule 8 says a PR touching ``tests/`` does not self-merge until a
reviewer's TESTS-READ note lands on it. The rule is satisfied by the note
*existing* — nothing looks at WHICH tree the note is a claim about. Measured on
one evening (2026-08-22): four PRs each carried a TESTS-READ note that read an
earlier head, and in every case ``tests/`` moved afterwards — #5090 (note at
``a7c44eef6``; the gate tests landed 3 and 15 minutes later), #5095 (note at
``e0dd40461``; the witness commit landed 13 minutes later), #5096 (note at
10:32; test files changed at 10:48 and 10:59), #5038 (note read ``5c90f2ac4``
while the PR head was ``6b5d587c0``, already merged). All four read green under
rule 8.

The class this closes is the one ``verification-hazards.md`` names as its root:
**an observation does not name its own referent**. So the predicate is not "was
the note any good" — nothing here reads the note's content. It is only:

1. the note names a SHA at all (absent -> red), and
2. no commit has touched ``tests/`` since that SHA (later commits -> red, with
   the commits listed, so the ask is "top up the diff", not "read it again"),
3. a ``tests/``-touching PR carries at least one note (none -> red) — rule 8
   itself, which nothing has been checking mechanically.

Deliberately NOT closed: an author can edit an old comment to swap in a newer
SHA. That is outside a text predicate, and pretending otherwise would be the
"a checklist item answered yes every time" shape CLAUDE.md already warns about.

stdlib-only (argparse / json / re / subprocess), mirroring
``scripts/check_pr_closing_intent.py`` so CI runs it dep-free.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys

#: A TESTS-READ note is recognised by this marker appearing in a PR comment.
#: Matched case-insensitively and tolerating the ``TESTS-READY`` typo that
#: several sessions produce, because the gate must not turn a typo into
#: "no note landed" (that would fail the PR for the wrong reason).
_NOTE_MARKER = re.compile(r"TESTS-READ(?:Y)?", re.IGNORECASE)

#: A 7-40 char hex run, the shape `git rev-parse` prints. Bounded on both sides
#: by a non-hex boundary so a longer word containing hex letters is not read as
#: a SHA. Backticks are the common wrapper in these notes and are not part of
#: the token.
_SHA = re.compile(r"(?<![0-9a-fA-F])([0-9a-fA-F]{7,40})(?![0-9a-fA-F])")

#: Words that would otherwise pass `_SHA` — all-hex English that shows up in
#: these notes. Compared lowercased.
_SHA_FALSE_FRIENDS = frozenset({
    "decade", "defaced", "faceted", "acceded", "effaced", "deface", "efface",
})


def find_note_shas(comment_body: str, known_oids: "list[str]") -> "list[str]":
    """The SHA-shaped tokens in *comment_body* that are actually commits of
    this PR (prefix-matched against *known_oids*).

    Membership is the whole point, not a nicety: a bare hex pattern also
    matches an issue-comment id (``5379476813`` is ten decimal digits, and
    decimal digits are hex digits), an all-hex English word ("decade",
    "faceted"), and any other long hex-ish run. Measured: the first live run of
    this gate read a comment id as a tree name and passed a PR it should have
    failed. A token that is not one of this PR's commits is not a claim about
    this PR's tree, so it is not a SHA for this gate's purposes."""
    out: "list[str]" = []
    for cand in _SHA.findall(comment_body):
        if cand.lower() in _SHA_FALSE_FRIENDS:
            continue
        if any(oid.startswith(cand) or cand.startswith(oid) for oid in known_oids if oid):
            out.append(cand)
    return out


def tests_commits_after(sha: str, head: str, commits: "list[dict]") -> "list[dict]":
    """The commits in *commits* that come strictly after *sha* and touch
    ``tests/``.

    *commits* is the PR's own commit list, oldest first, each with ``oid`` and
    the paths it touched (``_tests_paths``, injected by the caller — this
    function does no I/O so the fixture path and the live path share it).
    An unknown *sha* (not one of the PR's commits, e.g. a note citing a
    pre-branch base) yields every ``tests/``-touching commit: the note cannot
    be shown to cover any of them."""
    oids = [c.get("oid", "") for c in commits]
    start = -1
    for i, oid in enumerate(oids):
        if oid.startswith(sha) or sha.startswith(oid):
            start = i
            break
    tail = commits[start + 1:] if start >= 0 else commits
    return [c for c in tail if c.get("_tests_paths")]


def evaluate(pr: dict) -> "tuple[int, list[str]]":
    """``(exit_code, lines)`` for one PR payload.

    *pr* carries ``files`` (list of path strings), ``comments`` (list of
    ``{'body':}``), ``commits`` (oldest first, each ``{'oid':, '_tests_paths':}``)
    and ``headRefOid``. Pure — the live and fixture paths both build this
    shape first, so the decision is testable without GitHub."""
    touched_tests = [p for p in pr.get("files", []) if p.startswith("tests/")]
    if not touched_tests:
        return 0, ["OK — this PR does not touch tests/; rule 8 does not apply."]

    notes = [c for c in pr.get("comments", []) if _NOTE_MARKER.search(c.get("body", ""))]
    if not notes:
        return 1, [
            "RED — this PR touches tests/ but carries no TESTS-READ note.",
            "  House rule 8: a PR touching tests/ does not self-merge until a",
            "  reviewer's TESTS-READ note lands on it.",
            f"  tests/ files touched: {len(touched_tests)}",
        ]

    commits = pr.get("commits", [])
    oids = [c.get("oid", "") for c in commits] + [pr.get("headRefOid", "")]
    head = pr.get("headRefOid", "")
    lines: "list[str]" = []
    for note in notes:
        shas = find_note_shas(note.get("body", ""), oids)
        if not shas:
            continue
        for sha in shas:
            later = tests_commits_after(sha, head, commits)
            if not later:
                return 0, [
                    f"OK — a TESTS-READ note names {sha}, and no commit has",
                    "  touched tests/ since it.",
                ]
        lines.append(f"  note names {', '.join(shas)}")

    if not lines:
        return 1, [
            "RED — a TESTS-READ note landed, but none of them names a commit of",
            "  this PR. Add the head you read (e.g. `TESTS-READ (B) (head abc1234)`).",
            "  An issue-comment id is not a tree name — it is hex-shaped, and this",
            "  gate's first live run mistook one for a SHA.",
            "  Without it, nothing can tell whether the note is a claim about THIS tree.",
        ]

    stale: "list[str]" = []
    for note in notes:
        for sha in find_note_shas(note.get("body", ""), oids):
            for c in tests_commits_after(sha, head, commits):
                stale.append(f"  {c.get('oid', '')[:9]} {c.get('messageHeadline', '')[:60]}")
    return 1, [
        "RED — every TESTS-READ note reads a tree that tests/ has moved past.",
        *lines,
        "  tests/ commits after it:",
        *sorted(set(stale)),
        "  Ask the same reviewer for a DIFFERENTIAL note over just these commits —",
        "  re-reading the whole PR is not required.",
    ]


def fetch_pr(number: int) -> dict:
    """Build the `evaluate` payload for a live PR via `gh`."""
    raw = subprocess.run(
        ["gh", "pr", "view", str(number), "--json",
         "files,comments,commits,headRefOid"],
        capture_output=True, text=True, check=True,
    ).stdout
    data = json.loads(raw)
    commits = []
    for c in data.get("commits", []):
        oid = c.get("oid", "")
        shown = subprocess.run(
            ["git", "show", "--name-only", "--format=", oid],
            capture_output=True, text=True,
        )
        if shown.returncode != 0:
            # A commit this checkout cannot resolve (unfetched, or a squashed
            # merge) would otherwise read as "touched no tests" — a green for
            # the wrong reason, which is the exact shape this gate exists to
            # reject. Say so and stop instead.
            raise SystemExit(
                f"check_tests_read_names_its_tree: cannot resolve commit {oid} "
                f"in this checkout — fetch the PR's commits first "
                f"(`git fetch origin pull/{number}/head`). Refusing to report a "
                f"result computed from commits it could not read."
            )
        paths = shown.stdout.split()
        commits.append({
            "oid": oid,
            "messageHeadline": c.get("messageHeadline", ""),
            "_tests_paths": [p for p in paths if p.startswith("tests/")],
        })
    return {
        "files": [f.get("path", "") for f in data.get("files", [])],
        "comments": data.get("comments", []),
        "commits": commits,
        "headRefOid": data.get("headRefOid", ""),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Fail a tests/-touching PR whose TESTS-READ note does not name the "
            "tree it read, or names one tests/ has moved past."
        ),
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--pr", type=int, metavar="N", help="Live PR number, via gh.")
    group.add_argument(
        "--fixture", metavar="PATH",
        help=(
            "JSON file with keys 'files' (paths), 'comments' ([{'body':}]), "
            "'commits' (oldest first, [{'oid':, 'messageHeadline':, "
            "'_tests_paths':}]) and 'headRefOid'. Lets this run offline."
        ),
    )
    return parser


def main(argv: "list[str] | None" = None) -> int:
    args = build_parser().parse_args(argv)
    pr = (
        json.loads(open(args.fixture, encoding="utf-8").read())
        if args.fixture else fetch_pr(args.pr)
    )
    code, lines = evaluate(pr)
    print("\n".join(lines))
    return code


if __name__ == "__main__":
    sys.exit(main())
