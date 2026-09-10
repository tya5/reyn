#!/usr/bin/env python3
"""#6071 — a ratchet over audit-event kinds that have an individual
explanation row in `docs/reference/runtime/events.md` but do not name a
FIRING CONDITION anywhere in that row.

## Origin and scope — read lead-coder's #6071 ruling before touching this

docs-maintainer's own measurement (2026-09-10, `origin/main`): of 268
closed-vocabulary kinds (`AUDIT_EVENT_KINDS`, `event_schema.py`), 97 carry
an individual explanation row in `events.md`, and only 42 of those 97
(43%) name WHEN the event fires (`fires` / `only when` / `once per`,
etc.) rather than only its destination/payload. lead-coder's ruling: do
NOT mass-fix the other 55 — a firing condition written all at once, from
reading the code cold, is exactly as likely to be a WRONG guess as a
right one, and a wrong guess here has no "doc goes stale when the code
changes" safety net, because it was never true to begin with. The chosen
remedy is prospective only: stop a NEW kind from landing with the same
gap, via this gate, on the SAME PR that adds it.

## What this gate is, precisely — same shape as #6084's own gate

Like `scripts/user_facing_lang_gate.py`, this is a RATCHET, not a
one-time cleanup tool: the existing ~55 findings (however many THIS
script's own measurement produces — see "Baseline semantics" below) are
committed to a baseline and grandfathered; only a finding NOT already in
that baseline fails the gate. A contributor adding a new audit-event kind
and its own `events.md` row writes the firing-condition phrase (or
doesn't) AT THAT SAME row, so the population this gate can see (a kind's
own explanation row, searched for a firing-condition marker phrase
anywhere in its text) IS the population where a new violation would
land.

## Scope: marker-phrase presence, not correctness — and not full coverage

This gate answers ONE question: does a kind's own `events.md` row contain
at least one of a small set of firing-condition marker phrases somewhere
in its text? It does NOT check that the phrase is accurate, does NOT
check that the row is otherwise complete, and does NOT claim the 97-kind
"individually documented" population itself is exhaustive or even
correctly derived here — this script's own row-detection (a markdown
table row whose FIRST cell contains at least one backtick-quoted token
that is a real member of `AUDIT_EVENT_KINDS`) is an independent
re-measurement, expected to be CLOSE to docs-maintainer's own 97/55 count
but not necessarily identical (same "independently written scanners
converge approximately" precedent `user_facing_lang_gate.py`'s own module
docstring states for its own 89-finding baseline).

A kind with NO individual `events.md` row at all is OUT OF SCOPE for this
gate entirely (it is a different, already-known gap — `docs/reference/
runtime/events.md`'s own module-level completeness, not this gate's
subject) — this gate only ever flags a row that EXISTS and lacks a
marker, never an absent row.

## Marker phrases (case-insensitive substring match)

`fires`, `only when`, `once per`, `warn-once`, `emitted once`,
`triggered when`, `fired when`. Chosen to match docs-maintainer's own
disclosed method ("`fires` / `only when` / `once per` 等") plus 3 close
variants this script's own author found necessary to avoid materially
under-counting rows that clearly DO state a firing condition in different
words. This is a deliberately small, literal set — extending it is a
separate, reviewed decision (regenerate the baseline in the same PR),
never assumed by this docstring.

## Baseline semantics — same contract as `silent_except_ratchet.py`

The committed baseline is whatever THIS script's own logic measures
against the tree TODAY, not a hand-transcription of docs-maintainer's 55.
The two are expected to be close but not identical — see "Scope" above.

CI: gate
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_BASELINE_PATH = _ROOT / "scripts" / "audit_event_firing_condition_gate_baseline.json"
_EVENTS_DOC = _ROOT / "docs" / "reference" / "runtime" / "events.md"

# Firing-condition marker phrases — see module docstring's own "Marker
# phrases" section for why this exact set and nothing wider.
_MARKER_RE = re.compile(
    r"fires|only when|once per|warn-once|emitted once|triggered when|fired when",
    re.IGNORECASE,
)

# A markdown table row whose first cell is one or more backtick-quoted
# identifiers, comma-separated (matches both the single-kind rows and the
# "`a`, `b`, `c` | shared description" grouped-kind rows events.md uses).
_ROW_RE = re.compile(r"^\|\s*((?:`[a-zA-Z0-9_]+`(?:,\s*)?)+)\s*\|(.*)$")
_KIND_TOKEN_RE = re.compile(r"`([a-zA-Z0-9_]+)`")


def _closed_vocabulary() -> "frozenset[str]":
    """The event-kind population — DERIVED from the closed vocabulary
    (`event_schema.AUDIT_EVENT_KINDS`), never hand-maintained (lead-coder
    condition 1)."""
    sys.path.insert(0, str(_ROOT / "src"))
    from reyn.core.events.event_schema import AUDIT_EVENT_KINDS
    return frozenset(AUDIT_EVENT_KINDS)


def kind_rows(text: str, vocabulary: "frozenset[str]") -> "dict[str, list[str]]":
    """``{kind: [row_text, ...]}`` for every closed-vocabulary kind that
    has at least one markdown table row naming it in the row's first
    cell. A kind can accumulate more than one row's text if it is
    documented in more than one table (rare, but the marker search below
    checks ALL of them — a marker in ANY one row counts)."""
    rows: "dict[str, list[str]]" = {}
    for line in text.splitlines():
        if not line.startswith("|"):
            continue
        m = _ROW_RE.match(line)
        if not m:
            continue
        kinds_cell, rest = m.group(1), m.group(2)
        real_kinds = [
            k for k in _KIND_TOKEN_RE.findall(kinds_cell) if k in vocabulary
        ]
        for k in real_kinds:
            rows.setdefault(k, []).append(rest)
    return rows


def measured(root: Path = _ROOT) -> "tuple[set[str], int]":
    """``(finding_set, documented_kind_count)`` — a finding is a kind with
    at least one `events.md` row whose text (across every row it
    appears in) contains NO firing-condition marker phrase.
    ``documented_kind_count`` is the population this gate actually
    scanned (the "97" analogue) — the `scanned == 0` fail-closed check in
    `main` reads this, not a file count (this gate scans ONE file, so a
    file-count check would never be 0 as long as the file exists at all,
    the wrong fail-closed signal for this shape of gate)."""
    vocabulary = _closed_vocabulary()
    events_doc = root / "docs" / "reference" / "runtime" / "events.md"
    text = events_doc.read_text(encoding="utf-8")
    rows = kind_rows(text, vocabulary)
    findings = {
        kind for kind, row_texts in rows.items()
        if not any(_MARKER_RE.search(t) for t in row_texts)
    }
    return findings, len(rows)


def load_baseline(path: Path = _BASELINE_PATH) -> "set[str]":
    return set(json.loads(path.read_text(encoding="utf-8")))


def write_baseline(finding_set: "set[str]", path: Path = _BASELINE_PATH) -> None:
    path.write_text(json.dumps(sorted(finding_set), indent=2) + "\n", encoding="utf-8")


def new_findings(measured_set: "set[str]", baseline_set: "set[str]") -> "set[str]":
    """The ratchet check: a measured finding absent from baseline is new
    debt. A baselined finding that disappears from `measured` (someone
    added a firing-condition phrase, or removed the kind's row entirely)
    is silently allowed to drop — the same silent-shrink contract every
    ratchet in this repo shares."""
    return measured_set - baseline_set


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument(
        "--write-baseline",
        action="store_true",
        help=(
            "regenerate the baseline from the CURRENT measured findings "
            "instead of checking against it. Use for initial adoption, a "
            "real fix you want to lock in, or a deliberate reviewed new "
            "kind whose own firing condition you are intentionally "
            "deferring (say why in the PR body) — no comment-based "
            "exception escape hatch exists."
        ),
    )
    return parser


def main(argv: "list[str] | None" = None) -> int:
    args = build_parser().parse_args(argv)

    if not _EVENTS_DOC.exists():
        print(
            f"audit-event firing-condition gate FAILED: {_EVENTS_DOC} does "
            "not exist. This gate fails CLOSED on a missing scan target "
            "rather than silently reporting a clean population.",
            file=sys.stderr,
        )
        return 1

    try:
        current, scanned = measured(_ROOT)
    except (OSError, UnicodeDecodeError, ImportError) as exc:
        print(
            f"audit-event firing-condition gate FAILED: could not scan the "
            f"population ({exc}). This gate fails CLOSED on a scan error "
            "rather than silently under-counting — fix the scan, do not "
            "treat this as a pass.",
            file=sys.stderr,
        )
        return 1

    if scanned == 0:
        print(
            "audit-event firing-condition gate FAILED: the scan found 0 "
            "documented event kinds — this is a scanner failure (the row "
            "regex, or the AUDIT_EVENT_KINDS import, broke), not a clean "
            "population.",
            file=sys.stderr,
        )
        return 1

    if args.write_baseline:
        write_baseline(current, _BASELINE_PATH)
        print(
            f"Wrote {len(current)} finding(s) to {_BASELINE_PATH}, "
            f"{scanned} documented event kind(s) scanned in {_EVENTS_DOC}."
        )
        return 0

    baseline = load_baseline(_BASELINE_PATH)
    new = new_findings(current, baseline)

    if new:
        print("audit-event firing-condition gate FAILED:\n", file=sys.stderr)
        print(
            f"{len(new)} event kind(s) have a new {_EVENTS_DOC.relative_to(_ROOT)} "
            f"row with no firing-condition phrase, not in the baseline "
            f"({_BASELINE_PATH.relative_to(_ROOT)}):",
            file=sys.stderr,
        )
        for f in sorted(new):
            print(f"  {f}", file=sys.stderr)
        print(
            "\nA row documenting this event kind exists but names no "
            "firing condition (one of: fires / only when / once per / "
            "warn-once / emitted once / triggered when / fired when) — "
            "see scripts/audit_event_firing_condition_gate.py's own "
            "module docstring for exactly what this does and does not "
            "catch, and for the ~existing grandfathered findings. Add a "
            "firing-condition phrase to the row (WHEN the event fires, "
            "not just its destination/payload). If this is a deliberate, "
            "reviewed deferral, say so in the PR body and run "
            "--write-baseline.",
            file=sys.stderr,
        )
        return 1

    print(
        f"audit-event firing-condition gate OK: {len(current)} finding(s), "
        f"all baselined ({scanned} documented event kind(s) scanned). "
        "Covers only kinds with an existing events.md row — see module "
        "docstring for scope."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
