"""Tier 2: #1765 Step 1a-ii — SnapshotJournal.save routed off the event loop.

The per-mutation snapshot write+fsync is moved OFF the loop, through the SAME serial
DurabilityWorker as the WAL (``state_log.submit_durable``). Two invariants are pinned, plus
behavior-invariance:

  * loop-free   — a slow snapshot write no longer freezes the event loop (a concurrent
                  ticker keeps advancing). RED if ``save`` wrote inline (the pre-1a-ii sync
                  path).
  * WAL→snapshot ordering — the snapshot becomes durable only AFTER the WAL seq it records:
                  observed at snapshot-write time, that ``applied_seq`` is ALREADY on disk in
                  the WAL. RED if a mutation saved the snapshot before its WAL append (the
                  ordering that a crash would otherwise expose as a snapshot pointing at a
                  non-durable WAL entry).
  * persist + recover — a mutation's snapshot is durable + consistent with the WAL; a fresh
                  load after a simulated restart sees the mutated state at the right seq.

Real instances (no mocks): a real StateLog + a real SnapshotJournal.
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

from reyn.core.events.agent_snapshot import AgentSnapshot
from reyn.core.events.state_log import StateLog
from reyn.runtime.services.snapshot_journal import SnapshotJournal


def _journal(tmp_path: Path) -> tuple[SnapshotJournal, StateLog, Path]:
    snap_path = tmp_path / ".reyn" / "agents" / "alpha" / "state" / "snapshot.json"
    snap_path.parent.mkdir(parents=True, exist_ok=True)
    sl = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    j = SnapshotJournal(agent_name="alpha", snapshot_path=snap_path, state_log=sl)
    return j, sl, snap_path


def _wal_seqs(sl: StateLog) -> list[int]:
    p = Path(sl.path)  # the on-disk WAL file (public accessor)
    if not p.exists():
        return []
    return [json.loads(ln)["seq"] for ln in p.read_text().splitlines() if ln.strip()]


@pytest.mark.asyncio
async def test_snapshot_save_keeps_event_loop_free(tmp_path, monkeypatch):
    """Tier 2: a slow snapshot write runs OFF the loop — a concurrent ticker keeps advancing
    while a mutation's snapshot save is in its (slow) write+fsync. RED if save wrote inline on
    the loop (the pre-1a-ii sync ``self._snapshot.save`` path would block the ticker)."""
    j, sl, _ = _journal(tmp_path)

    orig = AgentSnapshot.write_durable

    def _slow_write(path, data):  # runs in to_thread when routed off-loop → loop stays free
        time.sleep(0.1)
        return orig(path, data)

    monkeypatch.setattr(AgentSnapshot, "write_durable", staticmethod(_slow_write))

    ticks = 0

    async def _ticker() -> None:
        nonlocal ticks
        for _ in range(20):
            await asyncio.sleep(0.005)
            ticks += 1

    ticker = asyncio.create_task(_ticker())
    await j.append_inbox(kind="msg", payload={"text": "hi"})
    assert ticks > 0, "the loop must keep running during the slow off-loop snapshot write"
    await ticker
    await sl.aclose()


@pytest.mark.asyncio
async def test_snapshot_durable_only_after_its_wal_seq(tmp_path, monkeypatch):
    """Tier 2: WAL→snapshot ordering. At the instant the snapshot is written, the
    ``applied_seq`` it records is ALREADY present in the on-disk WAL — i.e. the WAL append
    became durable BEFORE the snapshot save. RED if a mutation saved the snapshot before (or
    concurrently with) its WAL append: the snapshot would record a seq not yet (or never) in
    the WAL, which a crash mid-mutation would expose."""
    j, sl, _ = _journal(tmp_path)

    orig = AgentSnapshot.write_durable
    observations: list[tuple[int, list[int]]] = []

    def _observing_write(path, data):
        applied_seq = json.loads(data)["applied_seq"]
        observations.append((applied_seq, _wal_seqs(sl)))  # WAL state AT snapshot-write time
        return orig(path, data)

    monkeypatch.setattr(AgentSnapshot, "write_durable", staticmethod(_observing_write))

    await j.append_inbox(kind="msg", payload={"text": "a"})
    await j.record_chain_register(
        chain_id="c1",
        fields={"origin_agent": "alpha", "origin_depth": 0,
                "original_request": "r", "waiting_on": ["x"]},
    )
    await j.consume_inbox(msg_id=j.snapshot.inbox[0]["id"] if j.snapshot.inbox else "none")

    assert observations, "snapshot saves must have run through the off-loop write"
    for applied_seq, wal_at_write in observations:
        assert applied_seq in wal_at_write, (
            f"snapshot recorded applied_seq={applied_seq} but the WAL on disk was "
            f"{wal_at_write} at write time — the WAL append must be durable FIRST"
        )
    await sl.aclose()


@pytest.mark.asyncio
async def test_mutation_persists_and_recovers(tmp_path):
    """Tier 2: behavior-invariant + recovery. After mutations the durable snapshot reflects
    the mutated state at the latest WAL seq; a fresh load (a simulated restart) sees it. RED
    if the off-loop save dropped/raced a write (snapshot stale or applied_seq behind the WAL)."""
    j, sl, snap_path = _journal(tmp_path)

    await j.append_inbox(kind="msg", payload={"text": "first"})
    await j.append_inbox(kind="msg", payload={"text": "second"})
    await sl.aclose()

    on_disk = AgentSnapshot.load("alpha", snap_path)
    wal_max = max(_wal_seqs(sl))
    assert on_disk.applied_seq == wal_max, "durable snapshot must sit at the latest WAL seq"
    assert [m["kind"] for m in on_disk.inbox] == ["msg", "msg"]
    assert [m["payload"]["text"] for m in on_disk.inbox] == ["first", "second"]
