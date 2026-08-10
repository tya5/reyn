#!/usr/bin/env python3
"""#3879 S2/S5 — derive "unprocessed" (arc population minus disposition) mechanically.

## What this is

`scripts/flat_tests_arc_population.json` is a FROZEN snapshot of every flat
`tests/*.py` file name #3879 needs a disposition for — the 1,129 filenames
`flat_tests_baseline.json` named at commit `accdfd226` (#3883, "Stage 0 — a
ratchet that rejects new .py files landing flat in tests/"), the arc's true
starting population (lead-coder ruling, #4072 follow-up; see the section
below on the first, WRONG snapshot this replaced).
`scripts/flat_tests_disposition.json` is a single-writer artifact recording,
per flat file, exactly ONE of three outcomes: `moved` (with the real
destination `to`), `flat-by-decision` (stays flat, `reason` states WHY —
never a classification word like "Tier 4" or "no axis"), or `deleted`
(`reason` states which of the six questions it failed).

## The first snapshot was wrong — "add the two live artifacts" isn't the population

The very first version of `flat_tests_arc_population.json` was `disposition.
keys() | baseline` (81 + 37 = 118) — exactly the "combination that isn't a
sum" #3879 itself warns about (bare numbers don't compare; a union of two
LIVE, already-partial views is not a census of the arc's actual starting
point). #4066 having only existed since the previous night meant the
~1,011 files moved BEFORE it was created had zero disposition record and
were invisible to that union — the "complete" set was ~10% of the real
population. Caught live (lead-coder, #4072 review). Fixed by reading the
one artifact that actually recorded the true starting state: Stage 0's own
`flat_tests_baseline.json`, 1,129 files, committed before any bucket
migration touched the tree.

## Backfilling ~1,020 pre-#4066 moves — mechanical, no per-file judgment

`git log -M100% --diff-filter=R --name-status accdfd226..origin/main --
tests/` names every rename `git` detected in this window: old path, new
path, commit, commit subject (which carries the PR number). Chain-
resolving each Stage-0 filename through that rename graph to its FINAL
tracked location (a file can be renamed more than once) accounted for all
1,129: 1,101 moved (all resolving to a currently-tracked path — verified,
not assumed), 25 still flat (exactly the real `origin/main` baseline,
cross-checked), 3 genuinely deleted (not renamed — `test_2708_cred_check_
chokepoint.py` / `test_chat_grant_file_write_187.py`, each removed in the
same PR that removed the mechanism it tested; `test_session_shutdown_
acompletion_warning.py`, already recorded via #4059/#4066). No manual
per-file judgment was needed for the 1,021 backfilled `moved` entries — a
rename is a rename; `bundle: "backfill-3879-arc-start"` marks them as
mechanically derived rather than authored per-bundle like the other 81.

**#3879 closes when the unprocessed set is empty** — not when the flat
count reaches zero (a file can legitimately have no subject bucket and stay
flat forever, AS LONG AS that decision is recorded with a reason). This
script is the mechanical derivation of that stopping condition, so nobody
has to ask lead-coder "how many are left" — anyone runs this and reads the
answer.

## Why `unprocessed` is measured against the FROZEN snapshot, not the live baseline

The first version of this script computed `unprocessed = baseline -
disposition.keys()`, where `baseline` is `flat_tests_baseline.json` —
which SHRINKS every time a file moves (the ratchet's whole design). That
made the gate vacuous by construction: a migration PR that moves every
remaining flat file WITHOUT ever writing a disposition entry for it also
drives the baseline to empty, so `unprocessed` reaches 0 with `disposition`
never having recorded the reason for any of those moves — a terminal-state-
only assertion, exactly the shape #3879 itself names as a hazard. Caught
live (lead-coder, #4068 follow-up): #4066 merged with 0 new disposition
entries recorded for #4063/#4071's moves, and `unprocessed` still read 37
(not because 37 were newly accounted for, but because the *baseline*
shrank out from under the old population). Freezing the population at a
point-in-time snapshot removes the shrink: a file that moves without a
disposition entry stays in `arc_population`, unaccounted for, forever,
until someone actually writes the entry.

## Who writes a `moved` disposition entry — DECIDED, #4068 follow-up

Single-writer (this script's own author, e2e-coder) still owns
`flat_tests_disposition.json` itself, to avoid the shared-central-file
merge-conflict hazard a migration PR touching it in parallel with another
would hit. But a migration PR does NOT skip writing the entry — it is
TRANSCRIBED by the single writer in a follow-up PR before the arc is
considered to have made progress on those files, and `unprocessed` staying
non-empty against the frozen snapshot is exactly the visible signal that a
transcription PR is still owed. This was previously undecided (neither
"the mover writes it" nor "someone transcribes it after" was true in
practice) — this is the decision, recorded here since this script's own
docstring is the surface future migration PRs actually read.

## Why `unprocessed` can be non-empty even for a MOVED-and-transcribed file

A `moved` disposition can be recorded before its PR merges (a real decision
already exists, even if not yet landed) — `arc_population` doesn't change
either way (it's frozen), so a file with a recorded `moved` entry whose PR
hasn't merged yet is accounted for (has a disposition entry), not
unprocessed — correct: the outstanding work is "land the PR," not "decide
what to do with this file."
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_BASELINE_PATH = _ROOT / "scripts" / "flat_tests_baseline.json"
_DISPOSITION_PATH = _ROOT / "scripts" / "flat_tests_disposition.json"
_ARC_POPULATION_PATH = _ROOT / "scripts" / "flat_tests_arc_population.json"

_VALID_DISPOSITIONS = frozenset({"moved", "flat-by-decision", "deleted"})
_BANNED_REASON_WORDS = ("tier 4", "no axis", "軸が無い", "no dominant axis")


def load_baseline(path: Path = _BASELINE_PATH) -> "set[str]":
    """The baseline's bare filenames, normalized to `tests/<name>` — the
    same key shape `flat_tests_disposition.json` uses. Still used for the
    ratchet's own reporting (`main()`'s "baseline: N flat files" line);
    NOT used to derive `unprocessed` anymore (see module docstring)."""
    names = json.loads(path.read_text(encoding="utf-8"))
    return {f"tests/{n}" for n in names}


def load_arc_population(path: Path = _ARC_POPULATION_PATH) -> "set[str]":
    """The frozen, never-shrinking snapshot `unprocessed` is measured
    against."""
    return set(json.loads(path.read_text(encoding="utf-8")))


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
    arc_population_path: Path = _ARC_POPULATION_PATH,
    disposition_path: Path = _DISPOSITION_PATH,
) -> "set[str]":
    """Frozen arc-population keys with no disposition entry — #3879's own
    stopping condition when this returns the empty set. Deliberately
    measured against the FROZEN snapshot, not the live-shrinking baseline
    (see module docstring)."""
    arc_population = load_arc_population(arc_population_path)
    disposition = load_disposition(disposition_path)
    return arc_population - set(disposition.keys())


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
    arc_population_count = len(load_arc_population())
    print(f"baseline (current, ratchet): {baseline_count} flat files")
    print(f"arc population (frozen snapshot): {arc_population_count} files")
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
