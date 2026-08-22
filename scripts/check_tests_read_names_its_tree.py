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

#5138: the note's CLAIM line lives in a PR **comment**, and only its FIRST
LINE is ever read (see ``_NOTE_MARKER`` / ``evaluate``). That comment's line 1
must carry the marker AND the head SHA together, e.g.
``**[e2e-coder]** — TESTS-READ (B: independent) (head 9cc100605)``. The
GROUNDS behind the claim (six-questions answers, scope, limits) live from
line 2 onward and are never read.

This shape had two earlier revisions, both measured wrong:

- **Rev 1 (this file's own #5039 history):** the claim's home was ANY line of
  ANY comment. `find_note_shas` read a whole comment body, so prose that
  merely *discussed* TESTS-READ — with an unrelated SHA-shaped token nearby —
  could pass.
- **Rev 2 (#5138, briefly): the claim moved into the PR BODY.** A
  ``pull_request: edited`` event carries the PR's own head sha, so a body
  edit's check run lands where it needs to (an ``issue_comment`` run carries
  the DEFAULT BRANCH's sha instead — measured 4/4, #5127/#5128/#5132/#5136
  stayed red until a human re-ran the workflow by hand). But the PR body is
  one document serving many purposes at once (description, Test plan,
  reviewer blocking points, bootstrap notes) — measured on the PR that
  introduced this exact revision (#5144): the body discussed TESTS-READ in
  prose *and* a real commit SHA of the PR appeared elsewhere in that same
  prose (a fixing-commit reference in a checked-off blocking point), and the
  gate went green with no reviewer note at all. A comment is one document,
  one purpose, and this predicate now additionally requires the marker and
  the SHA to be co-located on that comment's OWN FIRST LINE — a document that
  is *about* TESTS-READ, rather than *stating* a TESTS-READ claim, does not
  put the marker there. This removes self-reference SYNTACTICALLY, not
  statistically: it is not that a marker-plus-SHA elsewhere in a multi-line
  comment is merely less likely to be read as a claim — it is structurally
  excluded, because ``evaluate`` never looks past a comment's first line.

Reporting: a check run attaches to the sha its *triggering event* carries,
and ``issue_comment`` (fired when a comment is posted — the event that has to
re-run this gate, since ``pull_request`` does not fire on a new comment)
carries the default branch's sha, not the PR's. So the CI workflow does not
rely on a check run at all; it posts a GitHub commit **status** to the PR's
own head sha, resolved once via ``gh pr view`` regardless of which event
triggered the run. One reporting channel, not two (architect's first
objection to this shape, on an earlier round: a check run and a commit
status both encoding "did this pass" is the exact class this repo spent an
evening closing). The workflow posts `pending` before running this script and
`success`/`failure` after — not decoration: it is what keeps a job that dies
mid-run from going silent (architect's second objection: a job forced to
always exit 0 so it never reds is a gate that fires and says nothing). See
``check-tests-read-names-its-tree.yml`` for the three-step shape that
provides this; it is not something a pure Python predicate can exercise, and
the test suite says so plainly rather than pretending to cover it.

Deliberately NOT closed: this predicate proves only that a comment's first
line names a fresh commit of this PR — never that a *reviewer* wrote it.
Every session in this repo authenticates as the same ``gh`` user (house
rule preamble, PR-workflow doc), so comment authorship cannot distinguish
reviewer from author any more than body authorship could; the syntactic
first-line restriction closes the #5144 false-green (a claim can no longer
hide inside prose that merely discusses the marker), but it does not and
cannot establish WHO posted the claim. Rule 8's actual review requirement is
enforced by a human reading the PR before merging, not by this check. Text
predicates over authorship are out of reach here, and pretending otherwise
would be the "a checklist item answered yes every time" shape CLAUDE.md
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

#: A TESTS-READ note's claim line is recognised by this marker appearing on a
#: comment's FIRST LINE (see ``_first_line`` / ``evaluate``) — never a line 2+
#: only appearance, and never the PR body. Matched case-insensitively and
#: tolerating the ``TESTS-READY`` typo that several sessions produce, because
#: the gate must not turn a typo into "no note landed" (that would fail the
#: PR for the wrong reason).
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


def _first_line(text: str) -> str:
    """*text*'s first line, ``\\n``-delimited, with no trailing newline.

    The gate's entire "read only the claim, not the grounds" boundary is
    this one split: a comment's line 2 onward (the six-questions write-up,
    scope, limits) is never passed to ``_NOTE_MARKER`` or ``find_note_shas``
    at all — not filtered out after being read, but never handed to either
    of them, which is what makes the exclusion syntactic rather than a
    matter of degree."""
    return text.split("\n", 1)[0]


def find_note_shas(note_line: str, known_oids: "list[str]") -> "list[str]":
    """The SHA-shaped tokens in *note_line* that are actually commits of
    this PR (prefix-matched against *known_oids*).

    Membership is the whole point, not a nicety: a bare hex pattern also
    matches an issue-comment id (``5379476813`` is ten decimal digits, and
    decimal digits are hex digits), an all-hex English word ("decade",
    "faceted"), and any other long hex-ish run. Measured: the first live run of
    this gate read a comment id as a tree name and passed a PR it should have
    failed. A token that is not one of this PR's commits is not a claim about
    this PR's tree, so it is not a SHA for this gate's purposes."""
    out: "list[str]" = []
    for cand in _SHA.findall(note_line):
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

    *pr* carries ``files`` (list of path strings), ``comments`` (list of
    ``{'body':}``), ``commits`` (oldest first, each ``{'oid':,
    '_tests_paths':}``) and ``headRefOid``. Pure — the live and fixture paths
    both build this shape first, so the decision is testable without GitHub.

    A claim is a comment whose FIRST LINE (``_first_line``) matches
    ``_NOTE_MARKER``; the SHA search (``find_note_shas``) also runs only over
    that first line, never the rest of the comment. A comment that mentions
    TESTS-READ only from its second line onward is not a claim at all — it
    falls into the same "no note" bucket as a comment that never mentions it,
    which is deliberate: a document *about* the marker is not a document
    *stating* the marker.

    What a green return means, precisely: A COMMENT'S FIRST LINE NAMES A
    FRESH COMMIT OF THIS PR. It does NOT mean a reviewer made that claim —
    every session here authenticates as the same ``gh`` user, so comment
    authorship carries no reviewer/author distinction for this function (or
    any text predicate) to read. Rule 8's actual review requirement is
    enforced by a human reading the PR before merging, not by this check;
    treat a green here as "the tree is named", never as "the tree was
    reviewed"."""
    touched_tests = [p for p in pr.get("files", []) if p.startswith("tests/")]
    if not touched_tests:
        return 0, ["OK — this PR does not touch tests/; rule 8 does not apply."]

    notes = [
        _first_line(c.get("body", ""))
        for c in pr.get("comments", [])
        if _NOTE_MARKER.search(_first_line(c.get("body", "")))
    ]
    if not notes:
        return 1, [
            "RED — this PR touches tests/ but carries no TESTS-READ note.",
            "  House rule 8: a PR touching tests/ does not self-merge until a",
            "  reviewer's TESTS-READ note lands on it — the marker and the head",
            "  SHA must both be on a comment's FIRST line; a marker mentioned",
            "  only from line 2 onward does not count as a note.",
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
        shas = find_note_shas(note, oids)
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
            "  this PR. Add the head you read (e.g. `TESTS-READ (B) (head abc1234)`),",
            "  on the SAME first line as the marker.",
            "  An issue-comment id is not a tree name — it is hex-shaped, and this",
            "  gate's first live run mistook one for a SHA.",
            "  Without it, nothing can tell whether the note is a claim about THIS tree.",
        ]

    stale: "list[str]" = []
    for note in notes:
        for sha in find_note_shas(note, oids):
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
         "files,comments,commits,headRefOid"],
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
            "JSON file with keys 'files' (paths), 'comments' ([{'body':}], "
            "only each one's first line is read as a possible claim), "
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
