#!/usr/bin/env python3
"""#3879 S2/S5 — derive "unprocessed" (baseline minus disposition) mechanically.

## What this is

`scripts/flat_tests_baseline.json` names every flat `tests/*.py` file right
now (the ratchet's own population). `scripts/flat_tests_disposition.json`
is a single-writer artifact recording, per flat file, exactly ONE of three
outcomes: `moved` (with the real destination `to`), `flat-by-decision`
(stays flat, `reason` states WHY — never a classification word like "Tier 4"
or "no axis"), or `deleted` (`reason` states which of the six questions it
failed).

**#3879 closes when the unprocessed set is empty** — not when the flat
count reaches zero (a file can legitimately have no subject bucket and stay
flat forever, AS LONG AS that decision is recorded with a reason). This
script is the mechanical derivation of that stopping condition, so nobody
has to ask lead-coder "how many are left" — anyone runs this and reads the
answer.

## Why unprocessed can be non-empty even for a MOVED file

A `moved` disposition can be recorded before its PR merges (a real decision
already exists, even if not yet landed) — but `scripts/flat_tests_baseline.json`
only reflects what's TRUE on `origin/main` right now. Until that PR merges,
the file is still flat in the baseline AND has a `moved` disposition
recorded — this script reports it as accounted-for (has a disposition
entry), not unprocessed, which is correct: the outstanding work is "land
the PR," not "decide what to do with this file."
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_BASELINE_PATH = _ROOT / "scripts" / "flat_tests_baseline.json"
_DISPOSITION_PATH = _ROOT / "scripts" / "flat_tests_disposition.json"

_VALID_DISPOSITIONS = frozenset({"moved", "flat-by-decision", "deleted"})
_BANNED_REASON_WORDS = ("tier 4", "no axis", "軸が無い", "no dominant axis")


def load_baseline(path: Path = _BASELINE_PATH) -> "set[str]":
    """The baseline's bare filenames, normalized to `tests/<name>` — the
    same key shape `flat_tests_disposition.json` uses."""
    names = json.loads(path.read_text(encoding="utf-8"))
    return {f"tests/{n}" for n in names}


def load_disposition(path: Path = _DISPOSITION_PATH) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def validation_errors(disposition: dict) -> "list[str]":
    """Schema violations — a disposition entry that doesn't carry what its
    own `disposition` value requires. Not a Tier judgment; a shape check."""
    errors = []
    for key, entry in disposition.items():
        d = entry.get("disposition")
        if d not in _VALID_DISPOSITIONS:
            errors.append(f"{key}: disposition {d!r} not one of {sorted(_VALID_DISPOSITIONS)}")
            continue
        if d == "moved":
            if not entry.get("to"):
                errors.append(f"{key}: disposition=moved requires a non-empty 'to'")
        else:
            reason = entry.get("reason", "")
            if not reason:
                errors.append(f"{key}: disposition={d} requires a non-empty 'reason'")
            elif any(w in reason.lower() for w in _BANNED_REASON_WORDS):
                errors.append(
                    f"{key}: reason {reason!r} names a CLASSIFICATION, not a WHY "
                    "(banned: 'Tier 4', 'no axis')"
                )
    return errors


def unprocessed(
    baseline_path: Path = _BASELINE_PATH, disposition_path: Path = _DISPOSITION_PATH
) -> "set[str]":
    """baseline keys with no disposition entry — #3879's own stopping
    condition when this returns the empty set."""
    baseline = load_baseline(baseline_path)
    disposition = load_disposition(disposition_path)
    return baseline - set(disposition.keys())


def main(argv: "list[str] | None" = None) -> int:
    del argv
    disposition = load_disposition()
    errors = validation_errors(disposition)
    if errors:
        print("SCHEMA ERRORS in flat_tests_disposition.json:", file=sys.stderr)
        for e in errors:
            print(f"  {e}", file=sys.stderr)
        return 1

    remaining = sorted(unprocessed())
    baseline_count = len(load_baseline())
    print(f"baseline: {baseline_count} flat files")
    print(f"disposition entries: {len(disposition)}")
    print(f"unprocessed: {len(remaining)}")
    if remaining:
        print("\nunprocessed files:")
        for r in remaining:
            print(f"  {r}")
    else:
        print("\n#3879 stopping condition met: unprocessed set is empty.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
