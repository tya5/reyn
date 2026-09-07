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
`_markers.MARKER_BLOCKING`'s match, and the gate said nothing at all for 12
minutes; the PR only stayed red by the accident of an unrelated
BLOCKING from a different reviewer. Before #5522, "no marker on this
comment" and "a marker written wrong" were the same silence — condition
C makes the second one loud. Scoped to the SAME first-non-empty-line
surface condition A's own regexes read (never the whole body — a
mid-body mention, including this docstring's OWN repeated use of the
word, must never trip it).

Same PR (#5522) also strips backtick/`*` decoration from a line before
either marker regex runs (`_markers.undecorated`, and — #5919 stage 2 — its
role-prefix-preserving sibling `_undecorated_for_marker_match`) — the
root cause of the specific incident above, not just its silence. Aligns
this gate with `check_tests_read_names_its_tree.py`'s own
already-permissive `_SHA` regex (architect ruling: "許容側が自然" —
people decorate SHAs and keywords by hand, a machine stripping it once
is cheaper than asking every writer never to).

## #5919 stage 2 — anchoring the two DECIDING markers, never the WATCHER

The marker regexes above (`_markers.MARKER_BLOCKING`/`_markers.MARKER_CLEARED`) used to
be unanchored `\b...` searches over an undecorated first line — #5919's
census found this let 4 ordinary-prose shapes (a negating sentence, a
backtick span, a `>` quote, mid-sentence mention) satisfy them exactly
as `check_tests_read_names_its_tree.py`'s pre-#5919-stage-1 regex could.
Both are now built with `scripts/_markers.role_prefixed_marker(...)`,
column-0-anchored exactly like `_markers.NOTE_MARKER` (#5919 stage 3:
both now live there, not one per gate).

`_NEAR_MISS_BARE_WORD` (condition C) is deliberately NOT anchored and
NOT routed through `_markers.py` — see its own docstring for why
anchoring the WATCHER that exists to catch what the DECIDERS miss would
eliminate the watcher's entire purpose.

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

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _markers  # noqa: E402 -- sibling module, see _markers.py's own docstring

#: An open (unchecked) red blocking checkbox line — unchanged from #5135.
_OPEN_BLOCK = re.compile(r"^[ \t]*[-*+][ \t]*\[[ \t]*\][ \t]*(?:\*\*[ \t]*)*🔴(.*)$", re.MULTILINE)

#: A CHECKED red blocking checkbox line (#5314) — same shape as _OPEN_BLOCK,
#: an `x`/`X` inside the brackets instead of whitespace.
_CHECKED_BLOCK = re.compile(r"^[ \t]*[-*+][ \t]*\[[xX]\][ \t]*(?:\*\*[ \t]*)*🔴(.*)$", re.MULTILINE)

#: A comment's first line raising or clearing a blocking point (#5314
#: condition A; #5919 stage 2: now column-0 anchored, via the SAME
#: `_markers.role_prefixed_marker` shape `check_tests_read_names_its_
#: tree.py`'s `_NOTE_MARKER` already uses). The marker keyword must OPEN
#: the line — bare, or immediately after the CLAUDE.md rule-2 role prefix
#: (`**[role]** — `) — never merely appear somewhere on it.
#:
#: #5919 (lead-coder census + architect ruling): the PRE-anchoring shape
#: (`\bBLOCKING...`, unanchored) let 4 prose shapes read as a real marker
#: — a NEGATING sentence ("my blocking is closed, thanks"), the keyword
#: inside a backtick span, inside a `>` quote, or mid-sentence — because a
#: bare `.search()` cannot tell a DECLARATION from a DISCUSSION of the
#: same word. Column-0 anchoring (with the co-located `(head <sha>)` this
#: gate already required) closes all four the same way #5919 stage 1
#: closed them for TESTS-READ/RE-READ/the subprocess-pin exemption: an
#: ordinary sentence about the topic does not open a comment's first line
#: with the literal keyword or a role prefix immediately followed by it.
#:
#: `IGNORECASE` is dropped (`flags=0`, overriding `role_prefixed_marker`'s
#: own IGNORECASE default) for the SAME reason the pre-#5919 regex already
#: gave: prose is far more likely to write "blocking" lowercase than a
#: deliberate marker is.
#: #5919 stage 3: moved to `_markers.py` (`MARKER_BLOCKING`/
#: `MARKER_CLEARED`) — this gate's own logic below reads THOSE names
#: directly (`_markers.MARKER_BLOCKING`), no local alias, so
#: `check_blocking_has_reread_note.py` (or any future consumer) has
#: nothing private here left to reach into; see `_markers.py`'s own
#: docstring for why sharing the compiled instance, not reconstructing
#: it per consumer, is what keeps every consumer from drifting apart.

#: #5522 — a bare word check for NEAR-MISS DETECTION (condition C).
#:
#: 🔴 #5919 stage 2 (architect ruling, explicit): deliberately NOT routed
#: through `_markers.role_prefixed_marker` and deliberately NOT anchored
#: to column 0 or to the marker's own "(head <sha>)" shape, unlike
#: `_markers.MARKER_BLOCKING`/`_markers.MARKER_CLEARED` directly above. Those two DECIDE
#: ("does a real marker exist here?" — YES must be earned, so they are
#: anchored against ordinary prose producing a false YES). This one
#: WATCHES FOR WHAT THE DECIDERS MISSED ("did a marker attempt land here
#: at all, even a malformed one?" — a NO-side safety net, so anchoring it
#: would remove the exact cases it exists to catch: a decorated/malformed
#: marker that `_markers.MARKER_BLOCKING`/`_markers.MARKER_CLEARED` correctly reject).
#: Anchoring this one would make it redundant with — and therefore
#: silently subsumed by — the deciders it is supposed to be watching,
#: which is exactly the "one incident, no signal for 12 minutes" failure
#: #5522 fixed. The distinguishing question (architect, #5919): "is this
#: regex on the YES-deciding side, or the YES-was-missed watching side?"
#: Only the former gets anchored.
#:
#: Matches inside "BLOCKING-CLEARED" too (the `-` after "BLOCKING" is a
#: non-word char, satisfying `\b` on both sides of the bare word) —
#: deliberate, not an oversight: a decorated BLOCKING-CLEARED that fails
#: `_markers.MARKER_CLEARED` must ALSO be caught, and this one check does both
#: without a second pattern. Case-sensitive, matching the deciding
#: markers' own IGNORECASE-dropped posture.
_NEAR_MISS_BARE_WORD = re.compile(r"\bBLOCKING\b")

#: #5522 (architect ruling on the issue thread): markdown decoration —
#: backtick code-spans and `**`/`*` emphasis — is stripped from a line
#: before the near-miss check (`_NEAR_MISS_BARE_WORD`) runs against it, and
#: from whatever follows a detected role prefix before either DECIDING
#: marker (`_markers.MARKER_BLOCKING`/`_markers.MARKER_CLEARED`) runs (see
#: `_undecorated_for_marker_match` below — the role prefix's OWN `**...**`
#: syntax must survive, or #5919's new anchoring could never match a
#: decorated marker that follows a real role prefix). "Align to the
#: permissive side" (architect, quoting the reasoning): people decorate
#: SHAs and keywords when they write by hand — a machine stripping
#: decoration once is cheaper than asking every human writer never to.
#: Neither a SHA nor the BLOCKING/BLOCKING-CLEARED keywords themselves
#: ever contain a backtick or asterisk, so stripping cannot turn a
#: non-marker line into a false-positive match.
#:
#: #5919 stage 3: moved to `_markers.py` (`DECORATION`/`undecorated`) —
#: this gate's own logic reads those names directly, no local alias.


def _undecorated_for_marker_match(line: str) -> str:
    """*line*, with :data:`_markers.DECORATION` stripped EVERYWHERE
    EXCEPT a leading CLAUDE.md role prefix, if one opens the line.

    #5919 stage 2: delegates to `_markers.undecorated_after_role_prefix`
    (promoted there, not left as a private helper here, once lead-coder's
    review found `check_blocking_has_reread_note.py` needed the SAME
    shape for the SAME reason — this gate's marker anchoring is not the
    only consumer of "strip decoration but keep a real role prefix
    literal"). See that function's own docstring for the full incident:
    a blanket strip over the WHOLE line would erase a real role prefix's
    own `**[...]** — ` syntax right along with any decoration AROUND the
    marker, so `_markers.role_prefixed_marker`'s anchor would never match
    a decorated, role-prefixed comment post-strip."""
    return _markers.undecorated_after_role_prefix(line, _markers.DECORATION)


def _normalize(text: str) -> str:
    """Whitespace-collapsed, stripped — the same "normalization is space-
    folding only" rule condition B (and now A) both use; never case-folded
    or punctuation-stripped, so a quote must still be a real quote."""
    return re.sub(r"\s+", " ", text).strip()


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


def _cleared_after(identifying_text: str, candidate_bodies: "list[str]", head: str) -> bool:
    """Does one of *candidate_bodies* clear *identifying_text* for *head*?

    #5919 stage 2 (house rule 7 unification): replaces `_resolves_via_body`
    for BOTH condition A (a BLOCKING comment's identifying line, searched
    only in LATER comments) and condition B-2 (a checked checkbox's own
    line text, searched in ALL comments) — the checkbox form used to
    resolve on bare substring presence alone (`_resolves_via_body`), which
    could not tell a comment quoting the line WHILE DISPUTING it apart from
    one quoting it while resolving it: substring presence carries zero
    information about which. A comment now resolves EITHER form only when
    its own first line opens with the anchored `BLOCKING-CLEARED (head
    <sha>)` marker, that head matches the PR's current one, and its body
    still contains *identifying_text* verbatim (whitespace-normalized) —
    quoting is the required FORM for resolution now, not merely evidence
    of it."""
    if not identifying_text:
        return False
    for candidate_body in candidate_bodies:
        first_line = _undecorated_for_marker_match(_markers.first_nonempty_line(candidate_body))
        cleared_match = _markers.MARKER_CLEARED.match(first_line)
        if not cleared_match:
            continue
        cleared_sha = cleared_match.group(1)
        if not (head and (head.startswith(cleared_sha) or cleared_sha.startswith(head))):
            continue  # names a stale head, or no head at all -- does not clear
        if _normalize(identifying_text) in _normalize(candidate_body):
            return True
    return False


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

    # Condition B-2 (#5314; #5919 stage 2 house rule 7 unification): a
    # checked checkbox needs a BLOCKING-CLEARED-marked comment (naming the
    # current head) quoting its own text verbatim — ticking alone, or a
    # comment quoting the line without the marker, is not a record.
    for line_text in _checked_lines(body):
        if not _cleared_after(line_text, comment_bodies, head):
            findings.append(
                "RED (body) — a checked blocking line (`- [x] 🔴`) has no "
                f"BLOCKING-CLEARED comment quoting it verbatim: {line_text!r}. "
                f"Post a comment starting 'BLOCKING-CLEARED (head "
                f"{head[:9] or '<sha>'})' whose body contains that exact "
                "line, whitespace differences aside."
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
        blocking_first_line = _undecorated_for_marker_match(_markers.first_nonempty_line(blocking_body))
        blocking_match = _markers.MARKER_BLOCKING.match(blocking_first_line)
        if not blocking_match:
            continue
        identifying = _identifying_line(blocking_body)
        if not _cleared_after(identifying, comment_bodies[i + 1:], head):
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
    # made the marker regex NOT match, and the gate said nothing at
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
        first_line = _markers.first_nonempty_line(comment_body)
        if not first_line:
            continue
        undecorated_first_line = _markers.undecorated(first_line)
        if not _NEAR_MISS_BARE_WORD.search(undecorated_first_line):
            continue  # no BLOCKING/BLOCKING-CLEARED word on this line at all
        marker_line = _undecorated_for_marker_match(first_line)
        if _markers.MARKER_BLOCKING.match(marker_line) or _markers.MARKER_CLEARED.match(marker_line):
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
