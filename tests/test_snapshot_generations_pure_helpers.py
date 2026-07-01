"""Tier 2: pure helpers in snapshot_generations.py.

  ``_abandoned_intervals(rewinds)``  — open intervals abandoned by the rewind chain
  ``_branch_of_seq(seq, abandoned)`` — branch_id owning a given seq
  ``_make_is_active(abandoned)``     — closure returning is-active predicate

These helpers underpin ADR-0038 Stage 1b–1e (time-travel correctness) and the
derived branch tree (Phase-2, #1533). All take plain Python lists; no disk I/O.
"""
from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from reyn.core.events.snapshot_generations import (
    ACTIVE_BRANCH_ID,
    _abandoned_intervals,
    _branch_of_seq,
    _make_is_active,
)

# ---------------------------------------------------------------------------
# _abandoned_intervals
# ---------------------------------------------------------------------------


def test_abandoned_intervals_empty_rewinds() -> None:
    """Tier 2: no rewinds → no abandoned intervals."""
    assert _abandoned_intervals([]) == []


def test_abandoned_intervals_single_rewind() -> None:
    """Tier 2: one rewind (R=10, N=5) → interval (5, 10)."""
    result = _abandoned_intervals([(10, 5)])
    assert (5, 10) in result


def test_abandoned_intervals_two_independent_rewinds() -> None:
    """Tier 2: two non-overlapping rewinds produce two independent intervals.

    (R=5, N=2) and (R=15, N=12) do not overlap — R=5 is not inside (12, 15).
    Both intervals are kept.
    """
    result = _abandoned_intervals([(5, 2), (15, 12)])
    assert (2, 5) in result
    assert (12, 15) in result


def test_abandoned_intervals_inner_rewind_subsumed_by_outer() -> None:
    """Tier 2: inner rewind whose R falls inside an outer interval is dropped.

    (R=20, N=3) abandons (3, 20).  Then (R=10, N=5): R=10 is inside (3, 20)
    → subsumed → only (3, 20) survives.
    """
    result = _abandoned_intervals([(10, 5), (20, 3)])
    assert (3, 20) in result
    assert (5, 10) not in result


def test_abandoned_intervals_returns_open_intervals() -> None:
    """Tier 2: interval endpoints are exclusive — the record seq itself (R) is NOT abandoned."""
    result = _abandoned_intervals([(10, 5)])
    assert (5, 10) in result


# ---------------------------------------------------------------------------
# _make_is_active
# ---------------------------------------------------------------------------


def test_make_is_active_no_abandoned_all_active() -> None:
    """Tier 2: no abandoned intervals → every seq is active."""
    is_active = _make_is_active([])
    for seq in (1, 5, 10, 100):
        assert is_active(seq) is True


def test_make_is_active_seq_inside_interval_not_active() -> None:
    """Tier 2: seq strictly inside an abandoned interval is not active."""
    is_active = _make_is_active([(5, 10)])
    assert is_active(7) is False


def test_make_is_active_seq_at_lower_boundary_is_active() -> None:
    """Tier 2: seq == lo boundary (open interval) is active."""
    is_active = _make_is_active([(5, 10)])
    assert is_active(5) is True


def test_make_is_active_seq_at_upper_boundary_is_active() -> None:
    """Tier 2: seq == hi boundary (open interval) is active."""
    is_active = _make_is_active([(5, 10)])
    assert is_active(10) is True


def test_make_is_active_seq_outside_interval_is_active() -> None:
    """Tier 2: seq before or after the interval is active."""
    is_active = _make_is_active([(5, 10)])
    assert is_active(3) is True
    assert is_active(12) is True


# ---------------------------------------------------------------------------
# _branch_of_seq
# ---------------------------------------------------------------------------


def test_branch_of_seq_no_abandoned_is_active_branch() -> None:
    """Tier 2: no intervals → all seqs belong to the active branch (id 0)."""
    for seq in (1, 7, 50):
        assert _branch_of_seq(seq, []) == ACTIVE_BRANCH_ID


def test_branch_of_seq_outside_interval_is_active() -> None:
    """Tier 2: seq outside all intervals belongs to active branch."""
    abandoned = [(5, 10)]
    assert _branch_of_seq(3, abandoned) == ACTIVE_BRANCH_ID
    assert _branch_of_seq(11, abandoned) == ACTIVE_BRANCH_ID


def test_branch_of_seq_inside_interval_returns_r() -> None:
    """Tier 2: seq inside (N=5, R=10) → branch_id = R = 10."""
    abandoned = [(5, 10)]
    assert _branch_of_seq(7, abandoned) == 10


def test_branch_of_seq_at_boundary_is_active() -> None:
    """Tier 2: open interval endpoints (lo/hi) belong to active branch."""
    abandoned = [(5, 10)]
    assert _branch_of_seq(5, abandoned) == ACTIVE_BRANCH_ID
    assert _branch_of_seq(10, abandoned) == ACTIVE_BRANCH_ID


def test_branch_of_seq_nested_intervals_returns_tightest() -> None:
    """Tier 2: seq in nested intervals returns the innermost (max N) branch.

    Outer interval (2, 20, R=20), inner interval (8, 12, R=12).
    seq=10 is inside both; tightest has max N=8 → R=12.
    """
    abandoned = [(2, 20), (8, 12)]
    assert _branch_of_seq(10, abandoned) == 12
