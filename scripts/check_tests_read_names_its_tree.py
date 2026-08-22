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

The note's CLAIM line (e.g. ``TESTS-READ (B) (head abc1234)``) lives in the PR
**body**, not a comment. #5138: GitHub attaches a check run to the sha the
triggering event carries, and an ``issue_comment`` event (fired when a
comment is posted) carries the DEFAULT BRANCH's sha, not the PR's head — a run
it starts can never land on the PR's own check rollup. Measured: 4 of 4 PRs
whose note arrived as a comment (#5127, #5128, #5132, #5136) stayed red until
a human re-ran the workflow by hand. A ``pull_request: edited`` event — which
fires when the PR body changes — carries the PR's own head, so the check lands
where it needs to. Comments remain the place for the note's GROUNDS (the
four-section reviewer write-up: six-questions answers, scope, limits) — only
the one-line claim moved. Comments are still fetched here, but only to give a
precise diagnostic when a claim landed in the wrong place (see ``evaluate``).

Deliberately NOT closed, and broader than "an author can edit the body to
swap in a newer SHA with no new review behind it": moving the claim line from
a comment to the body moved it onto the side rule 8 exists to check. A
comment has an author distinct from the PR's; this gate could at least infer
"someone other than the author wrote this" from that (weak, but a signal).
The PR body has no such distinction — the PR's own author writes and edits it
freely. So this gate establishes only that A CLAIM NAMES THIS TREE, never
that a reviewer made the claim; that authorship signal is not weakened by
this move, it is GONE. This is this repo's own named shape: a discriminator a
trust decision reads must not come from the side being classified. Rule 8's
actual review requirement rests on a human reading the PR before merging, not
on this check — a green here is not evidence that review happened, only that
some text in the body names a commit of this PR. Attempting to verify
authorship with a text predicate is out of reach here and would produce
exactly the "a checklist item answered yes every time" shape CLAUDE.md
already warns about.

stdlib-only (argparse / json / re / subprocess), mirroring
``scripts/check_pr_closing_intent.py`` so CI runs it dep-free.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys

#: A TESTS-READ note's CLAIM line is recognised by this marker appearing in
#: the PR **body** (#5138 — a check run lands on the sha the triggering event
#: carries, and only ``pull_request: edited``, which fires on a body edit,
#: carries the PR's own head; an ``issue_comment`` run carries the default
#: branch's sha and its result can never reach the PR). Matched
#: case-insensitively and tolerating the ``TESTS-READY`` typo that several
#: sessions produce, because the gate must not turn a typo into "no note
#: landed" (that would fail the PR for the wrong reason).
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


def find_note_shas(note_body: str, known_oids: "list[str]") -> "list[str]":
    """The SHA-shaped tokens in *note_body* that are actually commits of
    this PR (prefix-matched against *known_oids*).

    Membership is the whole point, not a nicety: a bare hex pattern also
    matches an issue-comment id (``5379476813`` is ten decimal digits, and
    decimal digits are hex digits), an all-hex English word ("decade",
    "faceted"), and any other long hex-ish run. Measured: the first live run of
    this gate read a comment id as a tree name and passed a PR it should have
    failed. A token that is not one of this PR's commits is not a claim about
    this PR's tree, so it is not a SHA for this gate's purposes."""
    out: "list[str]" = []
    for cand in _SHA.findall(note_body):
        if cand.lower() in _SHA_FALSE_FRIENDS:
            continue
        if any(oid.startswith(cand) or cand.startswith(oid) for oid in known_oids if oid):
            out.append(cand)
    return out


def tests_commits_after(sha: str, commits: "list[dict]") -> "list[dict]":
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

    *pr* carries ``files`` (list of path strings), ``body`` (str — the PR
    description, where the note's CLAIM line lives, #5138), ``comments``
    (list of ``{'body':}`` — where the note's GROUNDS, the reviewer's
    write-up, still lives, and read here only to produce a precise
    diagnostic when a claim landed there instead), ``commits`` (oldest
    first, each ``{'oid':, '_tests_paths':}``) and ``headRefOid``. Pure —
    the live and fixture paths both build this shape first, so the decision
    is testable without GitHub.

    What a green return means, precisely: A CLAIM IN THE BODY NAMES A FRESH
    COMMIT OF THIS PR. It does NOT mean a reviewer made that claim — the PR
    body is written and edited by the PR's own author, with no distinct
    authorship signal this function (or any text predicate) can read. Rule
    8's actual review requirement is enforced by a human reading the PR
    before merging, not by this check; treat a green here as "the tree is
    named", never as "the tree was reviewed"."""
    touched_tests = [p for p in pr.get("files", []) if p.startswith("tests/")]
    if not touched_tests:
        return 0, ["OK — this PR does not touch tests/; rule 8 does not apply."]

    body = pr.get("body", "") or ""
    notes = [{"body": body}] if _NOTE_MARKER.search(body) else []
    if not notes:
        comment_notes = [
            c for c in pr.get("comments", []) if _NOTE_MARKER.search(c.get("body", ""))
        ]
        if comment_notes:
            return 1, [
                "RED — a TESTS-READ note landed in a comment, not the PR body.",
                "  The claim line belongs in the body (e.g. `TESTS-READ (B) (head",
                "  abc1234)`); the write-up behind it can stay a comment. Reason:",
                "  a check run lands on the sha the triggering event carries, and",
                "  only an event that carries THIS PR's head (a body edit) can put",
                "  the result on this PR — a comment-triggered run's result lands",
                "  on the default branch instead and never reaches this PR.",
            ]
        return 1, [
            "RED — this PR touches tests/ but carries no TESTS-READ note.",
            "  House rule 8: a PR touching tests/ does not self-merge until a",
            "  reviewer's TESTS-READ note lands on the PR body.",
            f"  tests/ files touched: {len(touched_tests)}",
        ]

    commits = pr.get("commits", [])
    if not commits:
        return 1, [
            "RED — this PR touches tests/ but its commit list is empty.",
            "  Nothing here can be shown to have been read; refusing to pass a",
            "  note that names a tree this check cannot locate.",
        ]
    # ONLY the PR's own commits — deliberately NOT headRefOid. A note quoting
    # the head passes membership, and with an empty commit list there is then
    # nothing for `tests_commits_after` to find, so the PR goes green with
    # nothing read (docs-maintainer, #5120 B). The head is a commit of this PR
    # whenever the PR has any, so including it separately buys nothing and
    # costs exactly this hole.
    oids = [c.get("oid", "") for c in commits]
    lines: "list[str]" = []
    for note in notes:
        shas = find_note_shas(note.get("body", ""), oids)
        if not shas:
            continue
        for sha in shas:
            later = tests_commits_after(sha, commits)
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
            for c in tests_commits_after(sha, commits):
                stale.append(f"  {c.get('oid', '')[:9]} {c.get('messageHeadline', '')[:60]}")
    return 1, [
        "RED — every TESTS-READ note reads a tree that tests/ has moved past.",
        *lines,
        "  tests/ commits after it:",
        *sorted(set(stale)),
        "  Ask the same reviewer for a DIFFERENTIAL note over just these commits —",
        "  re-reading the whole PR is not required.",
    ]


def commit_touched_paths(oid: str, repo: str, run=subprocess.run) -> "list[str]":
    """The paths *oid* touched, read from GitHub rather than a checkout.

    Deliberately NOT ``git show``: reading the tree would force this workflow
    to check the PR out, and an ``issue_comment`` run checks out the default
    branch — measured, that is how this gate spent its first hour reddening
    #5123/#5125/#5127 for a reason that had nothing to do with their notes
    (architect's security lens on the same PR: checking out a contributor's
    ref to run a script is a shape worth not having at all). The commit list
    and its file names are DATA on the API side; taking them as data means the
    PR's own tree is never executed here.

    Raises ``SystemExit`` rather than returning empty on an API failure: an
    empty list reads as "touched no tests", a green computed from a commit
    this check could not read, which is the shape it exists to reject.

    *run* is a seam, not a mock hook: an authenticated network call is not a
    "cheaply constructible real instance", so the suite injects a small runner
    here rather than reaching a live API. Measured why that matters — with the
    real one, the reject-side test passed in CI because `gh` had no token, not
    because the commit was bogus: green for the wrong reason, in the test for
    the very behaviour this function exists to provide."""
    result = run(
        ["gh", "api", f"repos/{repo}/commits/{oid}", "--jq", "[.files[].filename]"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise SystemExit(
            f"check_tests_read_names_its_tree: cannot read commit {oid} from "
            f"{repo} ({result.stderr.strip()[:200]}). Refusing to report a "
            f"result computed from a commit it could not read."
        )
    return json.loads(result.stdout or "[]")


def fetch_pr(number: int) -> dict:
    """Build the `evaluate` payload for a live PR via `gh` — no checkout."""
    repo = subprocess.run(
        ["gh", "repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    raw = subprocess.run(
        ["gh", "pr", "view", str(number), "--json",
         "body,files,comments,commits,headRefOid"],
        capture_output=True, text=True, check=True,
    ).stdout
    data = json.loads(raw)
    commits = [
        {
            "oid": c.get("oid", ""),
            "messageHeadline": c.get("messageHeadline", ""),
            "_tests_paths": [
                f for f in commit_touched_paths(c.get("oid", ""), repo)
                if f.startswith("tests/")
            ],
        }
        for c in data.get("commits", [])
    ]
    return {
        "body": data.get("body", ""),
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
            "JSON file with keys 'body' (the PR description, where the "
            "note's claim line lives), 'files' (paths), 'comments' "
            "([{'body':}], read only for the wrong-place diagnostic), "
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
