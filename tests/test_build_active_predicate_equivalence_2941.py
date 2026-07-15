"""Tier 2: #2941 — ``build_active_predicate`` (one WAL scan, reused per-seq) must
return IDENTICAL visibility to per-seq ``is_active_seq`` (one WAL scan EACH call).

``build_active_predicate`` hoists ``_abandoned_intervals(_rewind_records(state_log))``
out of a per-message loop (the freeze fix, see ``Session._active_branch_history``).
Since that derivation depends only on the state_log's rewind records — never on
the seq being tested — the hoisted predicate is provably equivalent to calling
``is_active_seq`` per seq; this test verifies that equivalence directly against a
real ``StateLog`` across: no rewind, one rewind, nested/subsuming rewinds, and a
checkout-back resurrection (the branch-tree scenarios ADR-0038 Stage 1b-1e define).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.core.events.snapshot_generations import (
    build_active_predicate,
    checkout,
    is_active_seq,
)
from reyn.core.events.state_log import StateLog


def _assert_equivalent(state_log: StateLog, seqs: range) -> None:
    predicate = build_active_predicate(state_log)
    for seq in seqs:
        assert predicate(seq) == is_active_seq(state_log, seq), (
            f"build_active_predicate diverges from is_active_seq at seq={seq}"
        )


@pytest.mark.asyncio
async def test_no_rewind_all_active(tmp_path: Path) -> None:
    """Tier 2: with no rewind records every seq is active under both."""
    state_log = StateLog(tmp_path / "state.wal")
    for i in range(10):
        await state_log.append("step_completed")
    _assert_equivalent(state_log, range(1, 11))


@pytest.mark.asyncio
async def test_single_rewind_abandons_interval(tmp_path: Path) -> None:
    """Tier 2: one rewind — the abandoned (target, R) interval hidden under both."""
    state_log = StateLog(tmp_path / "state.wal")
    seqs = [await state_log.append("step_completed") for _ in range(10)]
    await checkout(state_log, target_seq=seqs[2])
    _assert_equivalent(state_log, range(1, state_log.current_seq + 1))


@pytest.mark.asyncio
async def test_nested_subsuming_rewinds(tmp_path: Path) -> None:
    """Tier 2: a rewind, then more turns, then a rewind that subsumes the first
    (nested abandonment) — equivalence holds through the composition."""
    state_log = StateLog(tmp_path / "state.wal")
    seqs = [await state_log.append("step_completed") for _ in range(6)]
    await checkout(state_log, target_seq=seqs[4])  # abandon a small tail first
    more = [await state_log.append("step_completed") for _ in range(4)]
    await checkout(state_log, target_seq=seqs[1])  # subsumes the first rewind entirely
    _assert_equivalent(state_log, range(1, state_log.current_seq + 1))
    assert more  # sanity: the intervening turns exist in the WAL


@pytest.mark.asyncio
async def test_checkout_back_resurrection(tmp_path: Path) -> None:
    """Tier 2: rewind, branch forward, then checkout BACK to the old (now-abandoned)
    tip — resurrects it. Equivalence holds across the resurrection."""
    state_log = StateLog(tmp_path / "state.wal")
    seqs = [await state_log.append("step_completed") for _ in range(6)]
    await checkout(state_log, target_seq=seqs[2])       # rewind: abandons seqs[3:6]
    for _ in range(3):
        await state_log.append("step_completed")         # new branch forward
    await checkout(state_log, target_seq=seqs[5])       # checkout back: resurrects seqs[3:6]
    _assert_equivalent(state_log, range(1, state_log.current_seq + 1))


@pytest.mark.asyncio
async def test_predicate_is_reusable_across_many_seqs(tmp_path: Path) -> None:
    """Tier 2: a single predicate instance (built once) answers correctly for
    MANY different seqs — the exact reuse pattern ``_active_branch_history`` relies
    on (build once per turn, apply per message)."""
    state_log = StateLog(tmp_path / "state.wal")
    seqs = [await state_log.append("step_completed") for _ in range(20)]
    await checkout(state_log, target_seq=seqs[9])
    predicate = build_active_predicate(state_log)
    expected = [is_active_seq(state_log, s) for s in range(1, state_log.current_seq + 1)]
    actual = [predicate(s) for s in range(1, state_log.current_seq + 1)]
    assert actual == expected
