#!/usr/bin/env python3
"""Tabulate `compaction_check` outcomes from chat events logs — #1128 axis-1 measurement gate.

The axis-1 removal decision (issue #1128) is gated on PRIMARY EVIDENCE: across
live chat sessions, does the BACKGROUND path (`_maybe_compact`) ever do real
compaction work (`outcome="triggering"`), or is all real compaction done by the
forced-sync pre-frame guard (`outcome="forced_sync"`)? If `triggering` ≈ 0 while
`forced_sync` carries the real work, the background path (axis-1) is redundant.

Outcome → path map (verified against
`src/reyn/chat/services/compaction_controller.py`, HEAD d71f544a):

  BACKGROUND `_maybe_compact` (axis-1, removal candidate):
    too_few_turns / below_min_batch / below_threshold / triggering
    (+ already_running — AMBIGUOUS, both paths emit it)
  FORCED-SYNC `force_compact_now` (axis-2 pre-frame guard, stays):
    forced_sync / forced_sync_no_turns
    (+ already_running — AMBIGUOUS)

Real compaction happens ONLY on `triggering` (axis-1) or `forced_sync` (axis-2).

Usage:
    python scripts/compaction_outcome_dist.py <events.jsonl> [<events2.jsonl> ...]
    python scripts/compaction_outcome_dist.py <dir>   # recurse *.jsonl under dir

Emits a JSON summary to stdout (redirect to a file for the #1128 record, then
cat+copy it — never hand-type the numbers).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

# outcome → which path emits it. "ambiguous" = emitted by both paths.
_BACKGROUND = {"too_few_turns", "below_min_batch", "below_threshold", "triggering"}
_FORCED_SYNC = {"forced_sync", "forced_sync_no_turns"}
_AMBIGUOUS = {"already_running"}
# the two outcomes that mean "real compaction actually ran":
_REAL_WORK = {"triggering": "axis1_background", "forced_sync": "axis2_forced_sync"}


def _iter_event_files(args: list[str]):
    for a in args:
        p = Path(a)
        if p.is_dir():
            yield from sorted(p.rglob("*.jsonl"))
        elif p.exists():
            yield p


def _path_of(outcome: str) -> str:
    if outcome in _BACKGROUND:
        return "background_axis1"
    if outcome in _FORCED_SYNC:
        return "forced_sync_axis2"
    if outcome in _AMBIGUOUS:
        return "ambiguous_both"
    return "unknown"


def collect(files) -> dict:
    by_outcome: dict[str, int] = {}
    by_path: dict[str, int] = {"background_axis1": 0, "forced_sync_axis2": 0,
                               "ambiguous_both": 0, "unknown": 0}
    files_seen = 0
    files_with_events = 0
    for f in files:
        files_seen += 1
        had = False
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ev.get("type") != "compaction_check":
                continue
            had = True
            data = ev.get("data", ev)  # outcome may be top-level or under data
            outcome = data.get("outcome") or ev.get("outcome") or "<none>"
            by_outcome[outcome] = by_outcome.get(outcome, 0) + 1
            by_path[_path_of(outcome)] += 1
        if had:
            files_with_events += 1
    total = sum(by_outcome.values())
    triggering = by_outcome.get("triggering", 0)
    forced_sync = by_outcome.get("forced_sync", 0)
    real_total = triggering + forced_sync
    return {
        "files_seen": files_seen,
        "files_with_compaction_check": files_with_events,
        "total_compaction_check": total,
        "by_outcome": dict(sorted(by_outcome.items(), key=lambda kv: -kv[1])),
        "by_path": by_path,
        "real_compaction": {
            "axis1_background_triggering": triggering,
            "axis2_forced_sync": forced_sync,
            "total": real_total,
            "axis1_share": (triggering / real_total) if real_total else None,
        },
        "interpretation_hint": (
            "axis1_share == 0 (with real_total > 0) => background path never did "
            "real compaction => axis-1 redundant. axis1_share is None => no real "
            "compaction observed at all => sample insufficient to decide."
        ),
    }


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    files = list(_iter_event_files(argv[1:]))
    if not files:
        print(json.dumps({"error": "no .jsonl files found", "args": argv[1:]}))
        return 1
    print(json.dumps(collect(files), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
