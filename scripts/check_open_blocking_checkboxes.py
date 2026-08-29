#!/usr/bin/env python3
"""Fail a PR whose blocking point was closed without a corroborating record
(#5314) — an open checkbox (#5135, unchanged), a checked checkbox nobody
commented on, or a BLOCKING comment nobody posted BLOCKING-CLEARED for.

## The measured bypass (#5311, one night, 2 real instances)

The original gate (#5135) read only the PR **body** for an open
`- [ ] 🔴` checkbox. Measured: an author can make it green two ways,
neither of which resolves anything —

1. **Delete the line.** The reviewer's `- [ ] 🔴 ...` vanishes in a body
   rewrite; the gate finds no open box and passes.
2. **Tick it.** `- [ ] 🔴` becomes `- [x] 🔴`; the gate finds no *open*
   box (it never looked at checked ones) and passes. The point's own
   content never changed.

Both are the same shape architect named for #5063's sibling defect: the
PR body is read once, at merge time, by whoever reviews it right then —
never again. A gate that only reads the CURRENT body cannot tell
"resolved" from "made to look resolved by editing the one place the
gate looks."

## Why comment-only (condition A alone) is not enough either

The first design considered was reading BLOCKING/BLOCKING-CLEARED
markers from PR **comments** only, ignoring the body entirely (mirroring
`check_tests_read_names_its_tree.py`). Falsified before implementation:
every real blocking point observed in this repo is raised by editing the
PR **body** (house rule 7 — "goes in the PR body... not only in a
review comment"), never by posting a bare `BLOCKING (head <sha>)`
comment with nothing else. A comment-only gate would read 0 raised
points on every PR that follows the existing convention and pass
vacuously — closing the deletion bypass by opening a much bigger one
(silence via omission, on 100% of PRs, not the rare deliberate edit).

## The three conditions (A ∨ B ∨ C)

**Condition A (comment)**: a comment whose first line matches the
`BLOCKING (head <sha>)` shape is unresolved unless some LATER comment's
first line matches `BLOCKING-CLEARED (head <sha>)`, naming the PR's
CURRENT head specifically (not a stale one — a new push after a
CLEARED comment reopens it, same ∃-over-current-head rule
`check_tests_read_names_its_tree.py` already uses, architect's own
addition), AND whose body contains — verbatim, whitespace-normalized —
the BLOCKING comment's own identifying line (its first non-empty line
after the marker). No new identifier is invented for this: reviewers
already quote the point they are closing back at the author; this gate
requires exactly that habit, mechanically.

**Condition B (body, extended from #5135)**: an OPEN `- [ ] 🔴` line
still fails, unchanged. NEW: a CHECKED `- [x] 🔴` line now also fails
unless some comment's body contains that line's own text verbatim
(whitespace-normalized) — the same "quote it back" rule as condition A,
reused rather than reinvented.

Why both, not just B: B alone still lets deletion through — a body
rewrite that removes the line entirely satisfies neither B-1 (not open)
nor B-2 (not checked, there is nothing left to check). A alone lets
omission through (see above). Only A ∨ B closes what #5311 measured
without opening a new hole in the direction just closed.

**Condition C (near-miss, #5522)**: a comment whose FIRST NON-EMPTY LINE
names `BLOCKING`/`BLOCKING-CLEARED` but does not parse as either marker
regex is RED — a THIRD failure mode neither A nor B could see. Real
incident (lead-coder, 2026-08-29): a BLOCKING comment's first line read
``**BLOCKING (head `9862413f0`)**`` — the backtick around the SHA broke
`_BLOCKING_MARKER`'s match, and the gate said nothing at all for 12
minutes; the PR only stayed red by the accident of an unrelated
BLOCKING from a different reviewer. Before #5522, "no marker on this
comment" and "a marker written wrong" were the same silence — condition
C makes the second one loud. Scoped to the SAME first-non-empty-line
surface condition A's own regexes read (never the whole body — a
mid-body mention, including this docstring's OWN repeated use of the
word, must never trip it).

Same PR (#5522) also strips backtick/`*` decoration from a line before
either marker regex runs (`_undecorated`) — the root cause of the
specific incident above, not just its silence. Aligns this gate with
`check_tests_read_names_its_tree.py`'s own already-permissive `_SHA`
regex (architect ruling: "許容側が自然" — people decorate SHAs and
keywords by hand, a machine stripping it once is cheaper than asking
every writer never to).

## What this does NOT buy (disclosed, not hidden)

**This is not authorization.** Every session in this repo authenticates
as the same `gh` user (CLAUDE.md's own preamble — `--json author`
cannot tell sessions apart), so "the CLEARED comment's author matches
the BLOCKING comment's author" is a check that is always vacuously true
and was rejected for exactly that reason (architect, falsified before
implementation).

What survives instead is a DIRECTION, not an identity check: a body
checkbox rewards removal (delete the line, gate goes green — the
resolution never has to be stated anywhere). This design punishes it
(delete the CLEARED comment that would resolve a BLOCKING comment, and
the gate has nothing to find — it stays red, not green). The remaining
gap, stated plainly: a BLOCKING comment itself can still be deleted
(GitHub allows comment deletion), and if it is, this gate has nothing
left to require a CLEARED counterpart for. That is a materially bigger,
more visible act than editing one's own PR body, and it leaves no
residue of even a false claim — but it is not mechanically prevented.
Closing it would mean establishing identity across sessions that share
one `gh` user, a separate, more expensive arc this PR does not attempt.

## Matching granularity (docs-maintainer's own implementation choice,
## disclosed per lead-coder's request — the design brief did not pin
## this to a specific granularity, only "a verbatim substring, same
## normalization as condition B")

A BLOCKING comment may be long — the point's full reasoning lives there,
mirroring how `check_tests_read_names_its_tree.py` keeps a note's
"grounds" out of what the gate reads. Requiring a CLEARED comment to
quote the ENTIRE BLOCKING body back would be onerous and brittle (any
rewording breaks it); requiring only that ANY word overlaps would match
almost anything. This gate takes the BLOCKING comment's own **first
non-empty line after its marker line** — the headline sentence every
observed instance in this repo already leads with — as the identifying
text a CLEARED comment must quote back, verbatim after whitespace
normalization. Same unit condition B already uses (one line).

stdlib-only (argparse / json / re / subprocess), mirroring
`check_pr_closing_intent.py`/`check_tests_read_names_its_tree.py` so CI
runs it dep-free.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

#: An open (unchecked) red blocking checkbox line — unchanged from #5135.
_OPEN_BLOCK = re.compile(r"^[ \t]*[-*+][ \t]*\[[ \t]*\][ \t]*(?:\*\*[ \t]*)*🔴(.*)$", re.MULTILINE)

#: A CHECKED red blocking checkbox line (#5314) — same shape as _OPEN_BLOCK,
#: an `x`/`X` inside the brackets instead of whitespace.
_CHECKED_BLOCK = re.compile(r"^[ \t]*[-*+][ \t]*\[[xX]\][ \t]*(?:\*\*[ \t]*)*🔴(.*)$", re.MULTILINE)

#: A comment's first line raising or clearing a blocking point (#5314
#: condition A). Requires the FULL `BLOCKING (head <sha>)` / `BLOCKING-
#: CLEARED (head <sha>)` shape co-located on one line — not the bare word
#: found anywhere on the line. Case-sensitive and unanchored otherwise
#: (a real comment's first line is `**[<role>]** — BLOCKING (head
#: <sha>)`, the SAME "role prefix before the marker" shape every comment
#: in this repo already uses, so the marker itself is not required to
#: start the line — only to be followed by its own `(head <sha>)`).
#:
#: architect's TESTS-READ catch, lead-coder's real-world falsification
#: (#5318, "gate側の読み（1件、非blocking）。" — a review comment
#: genuinely discussing whether something IS a blocking point, containing
#: the bare word with no `(head <sha>)` anywhere near it): an EARLIER,
#: looser version of this regex matched the bare word "BLOCKING" anywhere
#: on line 1, so an everyday sentence mentioning it ("my blocking is
#: closed") was miscounted as a formal raise, permanently reddening any
#: PR whose review prose happened to use the word this way. Requiring the
#: `(head <sha>)` immediately after the marker (whitespace only between)
#: is what makes this a FORM check, not a word search — the same
#: distinction `check_tests_read_names_its_tree.py`'s own marker+SHA
#: co-location rule draws on its claim side. `IGNORECASE` is dropped
#: (not just made stricter) for the same reason: prose is far more likely
#: to write "blocking" lowercase than a deliberate marker is.
_BLOCKING_MARKER = re.compile(r"\bBLOCKING(?!-CLEARED)\s*\(\s*head\s+([0-9a-fA-F]{7,40})\s*\)")
_CLEARED_MARKER = re.compile(r"\bBLOCKING-CLEARED\s*\(\s*head\s+([0-9a-fA-F]{7,40})\s*\)")

#: #5522 — a bare word check, deliberately NOT anchored to the marker's own
#: "(head <sha>)" shape: this is what NEAR-MISS DETECTION runs against, so
#: it must catch exactly the cases the marker regexes above miss. Matches
#: inside "BLOCKING-CLEARED" too (the `-` after "BLOCKING" is a non-word
#: char, satisfying `\b` on both sides of the bare word) — deliberate, not
#: an oversight: a decorated BLOCKING-CLEARED that fails _CLEARED_MARKER
#: must ALSO be caught, and this one check does both without a second
#: pattern. Case-sensitive, matching the marker regexes' own
#: IGNORECASE-dropped posture (module docstring: prose is more likely to
#: write "blocking" lowercase than a deliberate marker is).
_BARE_WORD = re.compile(r"\bBLOCKING\b")

#: #5522 (architect ruling on the issue thread): markdown decoration —
#: backtick code-spans and `**`/`*` emphasis — is stripped from a line
#: BEFORE either marker regex runs against it, aligning this gate's
#: previously-strict posture with `check_tests_read_names_its_tree.py`'s
#: own already-permissive one (that gate's `_SHA` regex tolerates a
#: backtick between "head" and the hex run structurally; this one's
#: `\s*` did not). "Align to the permissive side" (architect, quoting
#: the reasoning): people decorate SHAs and keywords when they write by
#: hand (lead-coder's own #5517 BLOCKING, architect's own separate
#: instance, both real, both this same night) — a machine stripping
#: decoration once is cheaper than asking every human writer never to.
#: Precedent: `check_claude_md_doc_overlap.py` (#4928) already strips
#: markdown decoration before tokenizing for the same reason. Neither
#: a SHA nor the BLOCKING/BLOCKING-CLEARED keywords themselves ever
#: contain a backtick or asterisk, so stripping cannot turn a
#: non-marker line into a false-positive match.
_DECORATION = re.compile(r"[`*]")


def _undecorated(line: str) -> str:
    return _DECORATION.sub("", line)


def _normalize(text: str) -> str:
    """Whitespace-collapsed, stripped — the same "normalization is space-
    folding only" rule condition B (and now A) both use; never case-folded
    or punctuation-stripped, so a quote must still be a real quote."""
    return re.sub(r"\s+", " ", text).strip()


def _first_nonempty_line(text: str) -> str:
    """The comment's first non-empty line — the SAME scope both the
    marker regexes and #5522's near-miss check read (architect ruling:
    "marker規則が既に読んでいる面と同じ面に限る" — near-miss detection
    must never widen the surface a formal marker match already uses,
    or it would fire on prose anywhere the marker regex is not also
    looking). A literal ``text.split("\\n", 1)[0]`` (this function's
    pre-#5522 shape) would silently miss a comment that opens with a
    blank line before its marker — a real gap this rename also closes,
    not just the near-miss feature's own scoping."""
    for line in text.split("\n"):
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


def _identifying_line(comment_body: str) -> str:
    """The BLOCKING comment's own first non-empty line AFTER its marker
    line — what a CLEARED comment must quote back. See the module
    docstring's "Matching granularity" section for why this unit."""
    lines = comment_body.split("\n")[1:]
    for line in lines:
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


def _checked_lines(body: str) -> "list[str]":
    return [_normalize(m.group(1)) for m in _CHECKED_BLOCK.finditer(body) if _normalize(m.group(1))]


def _resolves_via_body(text: str, comments: "list[str]") -> bool:
    normalized = _normalize(text)
    return any(normalized in _normalize(c) for c in comments if normalized)


def evaluate(pr: dict) -> "tuple[int, list[str]]":
    """``(exit_code, lines)`` for one PR payload.

    *pr* carries ``body`` (str), ``comments`` (list of ``{'body':}``) and
    ``headRefOid``. Pure — the live and fixture paths both build this shape
    first, so the decision is testable without GitHub."""
    body = pr.get("body")
    if not isinstance(body, str):
        return 2, ["PR body could not be fetched"]

    comment_bodies = [
        c.get("body", "") for c in pr.get("comments", []) if isinstance(c.get("body"), str)
    ]
    head = pr.get("headRefOid", "")

    findings: "list[str]" = []

    # Condition B-1: an open checkbox — unchanged from #5135.
    if _OPEN_BLOCK.search(body):
        findings.append(
            "RED (body) — PR body contains an open blocking checkbox "
            "(`- [ ] 🔴`)."
        )

    # Condition B-2 (#5314): a checked checkbox needs a comment quoting its
    # own text verbatim — ticking alone is not a record.
    for line_text in _checked_lines(body):
        if not _resolves_via_body(line_text, comment_bodies):
            findings.append(
                "RED (body) — a checked blocking line (`- [x] 🔴`) has no "
                f"comment quoting it verbatim: {line_text!r}. Post a comment "
                "containing that exact line, whitespace differences aside."
            )

    # Condition A (#5314): a BLOCKING comment needs a LATER CLEARED comment
    # naming the CURRENT head and quoting the BLOCKING comment's own
    # identifying line verbatim.
    #
    # A BLOCKING comment's OWN head is never checked against the current
    # one — intentional, not an oversight left unstated (lead-coder's
    # TESTS-READ catch, #5317: this was originally "correct by not
    # touching it", never written down as a decision). A raise does not
    # expire on a push: only a CLEARED comment's head must be current.
    # The opposite rule — treating a BLOCKING comment as stale once the
    # PR moves past its head — would let an ordinary push silently drop
    # an unresolved point with no deliberate action at all, worse than
    # the deletion bypass #5311 measured.
    for i, blocking_body in enumerate(comment_bodies):
        blocking_match = _BLOCKING_MARKER.search(_undecorated(_first_nonempty_line(blocking_body)))
        if not blocking_match:
            continue
        identifying = _identifying_line(blocking_body)
        resolved = False
        for later_body in comment_bodies[i + 1:]:
            cleared_match = _CLEARED_MARKER.search(_undecorated(_first_nonempty_line(later_body)))
            if not cleared_match:
                continue
            cleared_sha = cleared_match.group(1)
            if not (head and (head.startswith(cleared_sha) or cleared_sha.startswith(head))):
                continue  # names a stale head, or no head at all — does not clear
            if identifying and _normalize(identifying) in _normalize(later_body):
                resolved = True
                break
        if not resolved:
            findings.append(
                "RED (comment) — a BLOCKING comment has no matching "
                "BLOCKING-CLEARED comment naming the current head "
                f"{head!r} and quoting its identifying line verbatim: "
                f"{identifying!r}. Post a comment starting "
                f"'BLOCKING-CLEARED (head {head[:9] or '<sha>'})' whose body "
                "contains that line."
            )

    # Condition C (#5522) — near-miss detection. lead-coder's own real
    # incident: a BLOCKING comment's first line read
    # "**BLOCKING (head `9862413f0`)**" — the backtick around the SHA
    # made `_BLOCKING_MARKER` NOT match, and the gate said nothing at
    # all for 12 minutes; the PR only stayed red because of an
    # UNRELATED BLOCKING from another reviewer. "Marker not found" and
    # "marker written wrong" were indistinguishable failure modes.
    #
    # Scope is deliberately the SAME first-non-empty-line surface
    # condition A's own marker regexes read — never the whole comment
    # body (architect ruling: "契機を『本文にBLOCKINGの語が在る』にしな
    # いでください — 形式を論じる散文が全部鳴ります", concretely this
    # issue's OWN body and #5517's review comments, both of which
    # discuss the word "BLOCKING" without ever intending it as a
    # marker). A near-miss is real only when the word appears on the
    # SAME line a marker would have to be on to be read at all.
    for comment_body in comment_bodies:
        first_line = _first_nonempty_line(comment_body)
        if not first_line:
            continue
        undecorated_first_line = _undecorated(first_line)
        if not _BARE_WORD.search(undecorated_first_line):
            continue  # no BLOCKING/BLOCKING-CLEARED word on this line at all
        if _BLOCKING_MARKER.search(undecorated_first_line) or _CLEARED_MARKER.search(
            undecorated_first_line,
        ):
            continue  # a real marker, already handled by condition A above
        findings.append(
            "RED (near-miss) — a comment's first line names BLOCKING/"
            "BLOCKING-CLEARED but does not parse as the marker shape "
            f"'BLOCKING(-CLEARED) (head <sha>)': {first_line!r}. This "
            "comment was NOT read as a blocking point or a clear — if "
            "you meant it as one, fix the shape; if you meant prose "
            "discussing the word, keep it off the comment's first line."
        )

    if findings:
        return 1, findings
    return 0, [
        "OK — no open blocking checkbox, every checked line is corroborated "
        "by a comment, and every BLOCKING comment has a matching "
        "BLOCKING-CLEARED comment for the current head."
    ]


def fetch_pr(number: int) -> dict:
    """Build the ``evaluate`` payload for a live PR via ``gh`` — no checkout."""
    result = subprocess.run(
        ["gh", "pr", "view", str(number), "--json", "body,comments,headRefOid"],
        capture_output=True, text=True, check=True,
    )
    data = json.loads(result.stdout)
    return {
        "body": data.get("body"),
        "comments": data.get("comments", []),
        "headRefOid": data.get("headRefOid", ""),
    }


def run_gate(pr_supplier: Callable[[], dict]) -> int:
    """Evaluate a supplied PR payload, keeping retrieval as an explicit seam."""
    try:
        pr = pr_supplier()
    except Exception as exc:  # noqa: BLE001 - the gate must fail closed
        print(f"PR payload fetch failed: {exc}", file=sys.stderr)
        return 2
    code, lines = evaluate(pr)
    print("\n".join(lines))
    return code


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Fail a PR with an open blocking checkbox, a checked one nobody "
            "corroborated, or a BLOCKING comment nobody cleared."
        ),
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--pr", type=int, metavar="N", help="Live PR number, via gh.")
    group.add_argument(
        "--fixture", type=Path, metavar="PATH",
        help=(
            "JSON file with keys 'body' (str), 'comments' ([{'body':}]) and "
            "'headRefOid'. Lets this run offline."
        ),
    )
    return parser


def main(argv: "list[str] | None" = None) -> int:
    args = build_parser().parse_args(argv)
    if args.pr is not None:
        return run_gate(lambda: fetch_pr(args.pr))
    return run_gate(lambda: json.loads(args.fixture.read_text(encoding="utf-8")))


if __name__ == "__main__":
    raise SystemExit(main())
