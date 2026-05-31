"""Tests for scripts/compaction_outcome_dist.py — #1128 axis-1 measurement gate.

Tier 2: OS invariant — verifies the compaction_check outcome tabulator correctly
maps each outcome to its emitting path (background axis-1 vs forced-sync axis-2)
and isolates "real compaction" (triggering / forced_sync) from no-op checks.
Uses subprocess + hand-crafted JSONL events fixtures (no reyn imports needed).
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).parent.parent / "scripts" / "compaction_outcome_dist.py"


def _run(args: list[str]) -> tuple[str, int]:
    result = subprocess.run(
        [sys.executable, str(SCRIPT)] + args,
        capture_output=True, text=True,
    )
    return result.stdout + result.stderr, result.returncode


def _write_events(path: Path, outcomes: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for o in outcomes:
        lines.append(json.dumps({"type": "compaction_check", "data": {"outcome": o}}))
    # a non-compaction event must be ignored
    lines.append(json.dumps({"type": "phase_started", "data": {"phase": "x"}}))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_maps_outcomes_to_paths_and_isolates_real_compaction(tmp_path):
    """Tier 2: outcome→path map + real-compaction isolation (the #1128 metric)."""
    ev = tmp_path / "events" / "log.jsonl"
    _write_events(ev, [
        "too_few_turns", "below_min_batch", "below_threshold",  # background no-ops
        "triggering",                                            # axis-1 real work
        "forced_sync", "forced_sync",                            # axis-2 real work
        "already_running",                                       # ambiguous
    ])
    out, rc = _run([str(ev)])
    assert rc == 0, out
    d = json.loads(out)
    assert d["total_compaction_check"] == 7
    assert d["by_path"]["background_axis1"] == 4   # 3 no-ops + triggering
    assert d["by_path"]["forced_sync_axis2"] == 2
    assert d["by_path"]["ambiguous_both"] == 1
    assert d["real_compaction"]["axis1_background_triggering"] == 1
    assert d["real_compaction"]["axis2_forced_sync"] == 2
    assert d["real_compaction"]["total"] == 3
    assert abs(d["real_compaction"]["axis1_share"] - (1 / 3)) < 1e-9


def test_no_real_compaction_yields_null_share(tmp_path):
    """Tier 2: checks-fired-but-no-real-work => axis1_share=None (sample insufficient).

    Operationalizes the event-type-only-extrapolation trap: compaction_check
    firing != compaction happening.
    """
    ev = tmp_path / "events" / "log.jsonl"
    _write_events(ev, ["too_few_turns", "too_few_turns", "below_min_batch"])
    out, rc = _run([str(ev)])
    assert rc == 0, out
    d = json.loads(out)
    assert d["total_compaction_check"] == 3
    assert d["real_compaction"]["total"] == 0
    assert d["real_compaction"]["axis1_share"] is None


def test_recurses_directory_of_jsonl(tmp_path):
    """Tier 2: a directory arg recurses *.jsonl and aggregates."""
    _write_events(tmp_path / "a" / "s1.jsonl", ["triggering"])
    _write_events(tmp_path / "b" / "s2.jsonl", ["forced_sync"])
    out, rc = _run([str(tmp_path)])
    assert rc == 0, out
    d = json.loads(out)
    assert d["files_with_compaction_check"] == 2
    assert d["real_compaction"]["axis1_background_triggering"] == 1
    assert d["real_compaction"]["axis2_forced_sync"] == 1
