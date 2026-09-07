#!/usr/bin/env python3
"""Fail a PR that carries a BLOCKING comment but no TESTS-READ- or RE-READ-
shaped note naming its CURRENT head — house rule 7's `BLOCKING-CLEARED`
requires only
a comment quoting the raised point back (already enforced by
`check_open_blocking_checkboxes.py`), which any session can post about
its own fix; nothing until now required a SEPARATE record of someone
having engaged with the tree as it exists NOW (#5453).

## The gap (#5453, lead-coder)

House rule 8's `check_tests_read_names_its_tree.py` already gives this
repo exactly the record shape wanted — a comment's first line naming the
PR's CURRENT head, proven to be a real commit of the PR (never a stale
SHA, never an issue-comment id mistaken for one) — but its TRIGGER is
"does this PR touch `tests/``". A `src`-only PR that raised and cleared a
BLOCKING point has no mechanical trigger for a re-read record at all:
the author can post `BLOCKING-CLEARED` themselves, quoting the point back
verbatim (satisfying `check_open_blocking_checkboxes.py`'s own
condition A), and merge — with nobody's comment ever having named the
CURRENT tree.

architect's ruling (quoted by lead-coder on #5453): a PR with AT LEAST
ONE `BLOCKING` comment requires ONE re-read record, regardless of
whether it touches `tests/`. What the record must show: **that it
exists** — the SAME thing house rule 8 already asks for, never
AUTHORSHIP (every session in this repo authenticates as the same `gh`
user; CLAUDE.md's own preamble — an authorship check is always
vacuously true and was rejected for exactly that reason when
`check_open_blocking_checkboxes.py` was designed). Boundary: "did a
BLOCKING comment land", not every PR — the review round-trip this asks
for is a finite resource lead-coder does not want spent where no
blocking point was ever raised.

## Reused, not reinvented — plus one honest addition (architect, #5453)

This deliberately does NOT invent a new marker or a new comment shape for
the common case. A `TESTS-READ (head <sha>)`-marked comment naming the
PR's current head already IS "a record that someone engaged with this
exact tree" — accepting the SAME marker here (rather than a second,
parallel concept) means a PR that satisfies house rule 8 for an
unrelated reason (it also touches `tests/`) automatically satisfies this
rule too, with the same comment.

Architect's own non-blocking review of the first revision (quoted
verbatim): reusing `TESTS-READ` UNCONDITIONALLY would make a `src`-only
PR's re-read comment falsely claim "read a test that isn't in this diff
at all" — 「`tests/` を触らない PR で `TESTS-READ` を出すと『diff に無い
test を読んだ』と主張することになります」. `RE-READ (head <sha>)` is the
honest form for exactly that case; `_is_reread_note` accepts EITHER
marker, so a `tests/`-touching PR still clears both gates with ONE
`TESTS-READ` comment, while a `src`-only PR can post the accurate
`RE-READ` one instead of a claim it cannot back up.

`check_tests_read_names_its_tree.py` (for its own DECISION functions,
`find_note_shas`'s false-friend guard and `note_names_head`'s "names the
CURRENT head, not any past one" existential) and `_markers.py` (for
every RECOGNITION primitive both gates need — the BLOCKING/CLEARED and
TESTS-READ marker shapes, decoration-stripping, first-non-empty-line
isolation) are loaded via ``importlib.util.spec_from_file_location``
(mirroring `tests/scripts/test_check_tests_read_names_its_tree_5039.py`'s
own established technique for reaching a `scripts/`-local module — this
directory has no `__init__.py`, so a package import is not available).
#5919 stage 3 (architect's boundary ruling): a name read across module
boundaries is either recognition (`_markers.py`, this gate reads it
DIRECTLY, never through the module that also happens to use it) or
decision (the owning module's own PUBLIC name — never private, which is
the exact shape that broke silently between #5919 stages 2 and 3, see
`_has_blocking_comment`'s own docstring). `check_open_blocking_
checkboxes.py` is no longer loaded here at all: everything this gate
needed from it was recognition, and has moved to `_markers.py`.

## Report-only (lead-coder's own scoping, #5453)

NOT added to required status checks in this PR — lead-coder decides
that separately, after watching this run for real BLOCKING PRs without
first making every one of them un-mergeable on a script's first outing
(the same caution `check_tests_read_names_its_tree.py`'s own module
comment names for #5265's `startup_failure` exposure risk). The CI
workflow posts a commit status exactly like its sibling gate, just
under its own status context, so branch protection can be told to
require it later without touching this script.

stdlib-only (argparse / json / subprocess — the marker regexes and SHA
logic are IMPORTED, not reimplemented), mirroring
`check_pr_closing_intent.py`/`check_tests_read_names_its_tree.py` so CI
runs it dep-free.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent


def _load(module_name: str, filename: str):
    """Load a sibling ``scripts/`` module by file path (no ``__init__.py``
    here, so a package import is not available — mirrors
    ``tests/scripts/test_check_tests_read_names_its_tree_5039.py``'s own
    established technique for reaching a script-local module)."""
    spec = importlib.util.spec_from_file_location(module_name, _SCRIPTS_DIR / filename)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_tests_read = _load("_check_blocking_reread_tests_read", "check_tests_read_names_its_tree.py")
_markers = _load("_check_blocking_reread_markers", "_markers.py")

#: architect's non-blocking recommendation on #5453 (quoted verbatim below,
#: `evaluate`'s docstring) — reusing ONLY `_markers.NOTE_MARKER`
#: (`TESTS-READ`/`TESTS-READY`) would make a `src`-only PR's re-read
#: comment claim "read a test that isn't in this diff at all". `RE-READ
#: (head <sha>)` is the honest form for that case; either marker satisfies
#: THIS gate, so a PR that also touches `tests/` (and so already needs
#: `TESTS-READ` for house rule 8) still clears both gates with ONE
#: comment, while a `src`-only PR can post the accurate one.
#:
#: #5919: like `_markers.NOTE_MARKER`, now anchored to the CLAUDE.md
#: rule-2 role prefix via `_markers.role_prefixed_marker` — a bare
#: `.search()` used to let a line DENYING a re-read ("No RE-READ (head
#: start) needed here, this is a docs-only typo fix.") read as one.
#: Unlike TESTS-READ/BLOCKING/CLEARED, RE-READ is genuinely OWNED by this
#: gate alone — no other consumer means it — so it stays a private,
#: locally-built instance rather than moving to `_markers.py` (see that
#: module's own docstring for the recognition-vs-vocabulary boundary).
_RE_READ_MARKER = _markers.role_prefixed_marker(r"RE-READ\s*\(\s*head\s")


def _is_reread_note(first_line: str) -> bool:
    """True iff *first_line* matches EITHER accepted note marker — house
    rule 8's own `TESTS-READ`/`TESTS-READY` (reused unchanged, so a
    `tests/`-touching PR's existing note keeps satisfying this gate too),
    or this gate's own `RE-READ (head <sha>)` (architect's #5453
    recommendation, for a PR where "TESTS-READ" would misstate what was
    actually read).

    #5919 stage 3: reads `_markers.NOTE_MARKER` directly — NOT
    `_tests_read._NOTE_MARKER` (the private-borrow shape this whole
    issue exists to close) and not even `_tests_read.NOTE_MARKER` (that
    module no longer defines its own copy at all; it too now reads
    `_markers.NOTE_MARKER`, so there is nothing left to indirect
    through)."""
    return bool(
        _markers.NOTE_MARKER.search(first_line) or _RE_READ_MARKER.search(first_line)
    )


def _has_blocking_comment(comment_bodies: "list[str]") -> bool:
    """True iff ANY comment's first non-empty line matches the `BLOCKING
    (head <sha>)` shape (reused from `_markers.py` — the same anchored,
    role-prefix-aware marker #5919 stage 2 built and stage 3 promoted to
    a shared public instance, the same form check that excludes prose
    merely discussing the word, #5318's own false-positive fix).

    #5522/#5919: reads `_markers.first_nonempty_line`/`_markers.
    MARKER_BLOCKING`/`_markers.DECORATION` directly — NOT
    `_blocking._first_nonempty_line`/`_blocking._MARKER_BLOCKING`/
    `_blocking._DECORATION` (the private-borrow shape that broke silently
    between #5919 stages 2 and 3, when `check_open_blocking_checkboxes.
    py` renamed `_BLOCKING_MARKER` to `_MARKER_BLOCKING` and this gate's
    own borrow of the old name went stale, unnoticed by a scoped test run
    because the break sat outside stage 2's own diff). This gate shares
    the EXACT same real incidents fixed one level up, TWICE now: #5522's
    original (a BLOCKING comment whose SHA was backtick-wrapped would
    make the marker not match here EITHER, so THIS gate would silently
    conclude "no BLOCKING comment on this PR" and skip its own
    TESTS-READ/RE-READ requirement entirely), and #5919 stage 2's own
    (once the marker became role-prefix-anchored, a blanket strip would
    erase a REAL role prefix's own `**[...]** — ` literal right along
    with any decoration around the marker, breaking every role-prefixed
    BLOCKING comment — which is every real one in this repo, CLAUDE.md
    rule 2). Reusing the SAME shared instance (rather than a second copy)
    means a future change to it stays in sync with every consumer
    automatically."""
    return any(
        _markers.MARKER_BLOCKING.search(
            _markers.undecorated_after_role_prefix(
                _markers.first_nonempty_line(body), _markers.DECORATION,
            ),
        )
        for body in comment_bodies
    )


def evaluate(pr: dict) -> "tuple[int, list[str]]":
    """``(exit_code, lines)`` for one PR payload.

    *pr* carries ``comments`` (list of ``{'body':}``), ``commits`` (each
    ``{'oid':}``) and ``headRefOid`` — the SAME shape
    `check_tests_read_names_its_tree.evaluate` takes, minus ``files``
    (this gate's trigger is "has a BLOCKING comment", never "touches
    tests/"). Pure — the live and fixture paths both build this shape
    first, so the decision is testable without GitHub."""
    comment_bodies = [
        c.get("body", "") for c in pr.get("comments", []) if isinstance(c.get("body"), str)
    ]
    if not _has_blocking_comment(comment_bodies):
        return 0, ["OK — this PR carries no BLOCKING comment; #5453 does not apply."]

    notes = [
        _markers.first_nonempty_line(body)
        for body in comment_bodies
        if _is_reread_note(_markers.first_nonempty_line(body))
    ]
    if not notes:
        return 1, [
            "RED — this PR carries a BLOCKING comment but no TESTS-READ- or "
            "RE-READ-shaped note at all.",
            "  #5453: a PR with a raised BLOCKING point needs one comment naming",
            "  the tree that was re-read before merge — the marker and the head",
            "  SHA must both be on a comment's FIRST line: `TESTS-READ (head",
            "  abc1234)` (same shape house rule 8 already uses — one comment then",
            "  satisfies both gates on a PR that also touches tests/) or, for a",
            "  PR that does not touch tests/ at all, `RE-READ (head abc1234)`",
            "  (architect, #5453 — TESTS-READ would misstate what was read).",
        ]

    commits = pr.get("commits", [])
    if not commits:
        return 1, [
            "RED — this PR carries a BLOCKING comment but its commit list is "
            "empty.",
            "  Nothing here can be shown to have been read; refusing to pass a",
            "  note that names a tree this check cannot locate.",
        ]
    oids = [c.get("oid", "") for c in commits]
    head = pr.get("headRefOid", "")
    if not head:
        return 1, [
            "RED — this PR's headRefOid is empty; cannot verify a note names",
            "  the current tree.",
        ]

    resolved_count = 0
    stale: "list[tuple[str, list[str]]]" = []
    for note in notes:
        shas = _tests_read.find_note_shas(note, oids)
        if not shas:
            continue
        resolved_count += 1
        if _tests_read.note_names_head(note, head, oids):
            return 0, [f"OK — a re-read note names the current head {head}."]
        stale.append((note, shas))

    if not resolved_count:
        return 1, [
            "RED — a TESTS-READ- or RE-READ-shaped note landed, but none of "
            "them names a commit of this PR.",
            "  Add the head you read (e.g. `TESTS-READ (head abc1234)` or",
            "  `RE-READ (head abc1234)`), on the SAME first line as the marker.",
        ]

    lines = [
        "RED — this PR has a BLOCKING comment, but no re-read note names",
        f"  the current head {head}.",
        "  (∃ over the CURRENT head — an older note naming an earlier commit",
        "  does not count against a PR that has since moved. Post a FRESH note",
        "  naming this head; earlier notes stay exactly as posted.)",
    ]
    for note_text, shas in stale:
        lines.append(f"  note (first line): {note_text!r} — names {shas!r}, not the current head")
    return 1, lines


def fetch_pr(number: int) -> dict:
    """Build the ``evaluate`` payload for a live PR via ``gh`` — no checkout."""
    raw = subprocess.run(
        ["gh", "pr", "view", str(number), "--json", "comments,commits,headRefOid"],
        capture_output=True, text=True, check=True,
    ).stdout
    data = json.loads(raw)
    commits = [{"oid": c.get("oid", "")} for c in data.get("commits", [])]
    return {
        "comments": data.get("comments", []),
        "commits": commits,
        "headRefOid": data.get("headRefOid", ""),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Fail a PR with a BLOCKING comment but no TESTS-READ- or RE-READ-"
            "shaped note naming the tree it read (#5453)."
        ),
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--pr", type=int, metavar="N", help="Live PR number, via gh.")
    group.add_argument(
        "--fixture", type=Path, metavar="PATH",
        help=(
            "JSON file with keys 'comments' ([{'body':}]), 'commits' "
            "([{'oid':}]) and 'headRefOid'. Lets this run offline."
        ),
    )
    return parser


def main(argv: "list[str] | None" = None) -> int:
    args = build_parser().parse_args(argv)
    pr = (
        json.loads(args.fixture.read_text(encoding="utf-8"))
        if args.fixture else fetch_pr(args.pr)
    )
    code, lines = evaluate(pr)
    print("\n".join(lines))
    return code


if __name__ == "__main__":
    sys.exit(main())
