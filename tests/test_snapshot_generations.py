"""Tier 2: OS invariant — PITR reconstruct(N) equals a full WAL replay to N.

ADR-0038 Stage 1a. Real `StateLog` + real `AgentSnapshot` (no mocks): the
generation store + `reconstruct` must rebuild the exact same state whether it
starts from a snapshot generation or from empty + full WAL. Crash recovery is
the `reconstruct(head)` special case.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.events.agent_snapshot import AgentSnapshot
from reyn.events.snapshot_generations import SnapshotGenerationStore, reconstruct
from reyn.events.state_log import StateLog

AGENT = "alpha"


async def _build_wal(tmp_path: Path) -> StateLog:
    """A WAL exercising several agent-affecting kinds for AGENT (+ noise)."""
    log = StateLog(tmp_path / "state.wal")
    await log.append("inbox_put", target=AGENT, msg_id="m1", msg_kind="user",
                     payload={"text": "a"})            # seq 1
    await log.append("inbox_put", target="other", msg_id="x1", msg_kind="user",
                     payload={})                        # seq 2 (different agent)
    await log.append("chain_register", agent=AGENT, chain_id="c1",
                     origin_agent=AGENT, origin_depth=0,
                     original_request="r", waiting_on=["beta"])  # seq 3
    await log.append("inbox_put", target=AGENT, msg_id="m2", msg_kind="user",
                     payload={"text": "b"})            # seq 4
    await log.append("inbox_consume", target=AGENT, msg_id="m1")  # seq 5
    await log.append("chain_resolve", agent=AGENT, chain_id="c1")  # seq 6
    return log


def _full_replay(log: StateLog, target_seq: int) -> AgentSnapshot:
    """Ground truth: empty snapshot + every WAL entry with seq <= target_seq."""
    snap = AgentSnapshot.empty(AGENT)
    snap.apply_events(
        [e for e in log.iter_from(1)
         if isinstance(e.get("seq"), int) and e["seq"] <= target_seq]
    )
    return snap


@pytest.mark.asyncio
async def test_reconstruct_matches_full_replay_at_every_seq(tmp_path):
    """Tier 2: for every target N, reconstruct(N) == full-replay-to-N.

    Generations are cut at boundary seqs 1 and 4; reconstruct must agree with a
    from-empty replay regardless of which generation it bases on.
    """
    log = await _build_wal(tmp_path)
    store = SnapshotGenerationStore(AGENT, tmp_path / AGENT / "generations")
    # Cut generations at two boundaries (full replays to those seqs).
    store.record(_full_replay(log, 1))
    store.record(_full_replay(log, 4))

    for n in range(0, 7):
        got = reconstruct(AGENT, store, log, n)
        expected = _full_replay(log, n)
        assert got == expected, f"reconstruct({n}) diverged from full replay"


@pytest.mark.asyncio
async def test_reconstruct_head_is_crash_recovery(tmp_path):
    """Tier 2: reconstruct(head) == full replay to current_seq (crash recovery)."""
    log = await _build_wal(tmp_path)
    store = SnapshotGenerationStore(AGENT, tmp_path / AGENT / "generations")
    store.record(_full_replay(log, 4))
    head = log.current_seq
    assert reconstruct(AGENT, store, log, head) == _full_replay(log, head)


@pytest.mark.asyncio
async def test_reconstruct_with_no_generations_uses_empty_base(tmp_path):
    """Tier 2: empty store ⇒ reconstruct replays from empty + full WAL delta."""
    log = await _build_wal(tmp_path)
    store = SnapshotGenerationStore(AGENT, tmp_path / AGENT / "generations")
    assert store.seqs() == []
    head = log.current_seq
    assert reconstruct(AGENT, store, log, head) == _full_replay(log, head)


@pytest.mark.asyncio
async def test_generation_base_is_used_not_just_empty(tmp_path):
    """Tier 2: a generation at seq 4 is the base for reconstruct(5).

    Recording a generation whose applied_seq is 4 must make it the
    `nearest_at_or_below(5)` base — and the result still matches full replay.
    """
    log = await _build_wal(tmp_path)
    store = SnapshotGenerationStore(AGENT, tmp_path / AGENT / "generations")
    store.record(_full_replay(log, 1))
    store.record(_full_replay(log, 4))
    assert store.nearest_at_or_below(5) == 4
    assert store.nearest_at_or_below(3) == 1
    assert store.nearest_at_or_below(0) is None
    assert reconstruct(AGENT, store, log, 5) == _full_replay(log, 5)


@pytest.mark.asyncio
async def test_prune_below_drops_old_generations(tmp_path):
    """Tier 2: prune_below GCs generations below the floor (retention primitive)."""
    log = await _build_wal(tmp_path)
    store = SnapshotGenerationStore(AGENT, tmp_path / AGENT / "generations")
    store.record(_full_replay(log, 1))
    store.record(_full_replay(log, 4))
    store.record(_full_replay(log, 6))
    assert store.seqs() == [1, 4, 6]
    dropped = store.prune_below(4)
    assert dropped == 1
    assert store.seqs() == [4, 6]
    # Surviving generations still reconstruct correctly.
    assert reconstruct(AGENT, store, log, 6) == _full_replay(log, 6)
