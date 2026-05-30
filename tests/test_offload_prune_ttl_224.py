"""Tier 2: FP-0008 #224 — control_ir offload scratch-dir TTL pruning.

C5 (#1093) writes oversized control_ir_results to per-run scratch dirs
``{state_dir}/control_ir_offload/<run_id>/``. Without pruning these accumulate
one directory per run forever. ``prune_stale_offload_dirs`` removes per-run
subdirs older than a TTL (by mtime), preserving active + recently-completed
(resume-reachable) runs.

Pins (no mocks; deterministic via the injectable ``now``):
  (a) subdirs older than the TTL are removed; recent ones are kept;
  (b) a missing root is a defensive no-op (returns 0);
  (c) non-directory entries + the boundary are handled correctly.
"""
from __future__ import annotations

import os
from pathlib import Path

from reyn.services.offload import (
    DEFAULT_OFFLOAD_TTL_SECONDS,
    prune_stale_offload_dirs,
)


def _make_run_dir(root: Path, run_id: str, *, age_seconds: float, now: float) -> Path:
    d = root / run_id
    d.mkdir(parents=True)
    (d / "0000_abc.json").write_text("payload", encoding="utf-8")
    mtime = now - age_seconds
    os.utime(d, (mtime, mtime))
    return d


def test_prune_removes_stale_keeps_recent(tmp_path: Path) -> None:
    """Tier 2: (a) stale run dirs pruned, recent ones preserved (TTL by mtime)."""
    root = tmp_path / "control_ir_offload"
    root.mkdir()
    now = 1_000_000.0
    ttl = 3600.0

    stale = _make_run_dir(root, "run_old", age_seconds=ttl + 60, now=now)
    fresh = _make_run_dir(root, "run_new", age_seconds=ttl - 60, now=now)

    removed = prune_stale_offload_dirs(root, ttl_seconds=ttl, now=now)

    assert removed == 1
    assert not stale.exists()
    assert fresh.exists()
    assert (fresh / "0000_abc.json").read_text(encoding="utf-8") == "payload"


def test_prune_missing_root_is_noop(tmp_path: Path) -> None:
    """Tier 2: (b) a non-existent root prunes nothing and does not raise."""
    assert prune_stale_offload_dirs(tmp_path / "does_not_exist") == 0


def test_prune_ignores_non_directory_entries(tmp_path: Path) -> None:
    """Tier 2: (c) stray files under the root are left untouched."""
    root = tmp_path / "control_ir_offload"
    root.mkdir()
    now = 1_000_000.0
    stray = root / "stray.txt"
    stray.write_text("x", encoding="utf-8")
    os.utime(stray, (now - 10 * DEFAULT_OFFLOAD_TTL_SECONDS,) * 2)

    removed = prune_stale_offload_dirs(root, now=now)

    assert removed == 0
    assert stray.exists()


def test_prune_default_ttl_keeps_just_created_dir(tmp_path: Path) -> None:
    """Tier 2: a freshly-created run dir survives the default-TTL prune."""
    root = tmp_path / "control_ir_offload"
    root.mkdir()
    current = root / "run_current"
    current.mkdir()
    (current / "data.json").write_text("live", encoding="utf-8")

    assert prune_stale_offload_dirs(root) == 0
    assert current.exists()
