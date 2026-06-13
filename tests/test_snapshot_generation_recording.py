"""Tier 2: SnapshotJournal cuts PITR generations at boundaries (single seam).

ADR-0038 Stage 1a part-2. Real `SnapshotJournal` + `StateLog` +
`SnapshotGenerationStore` (no mocks): generation cuts happen only at the
boundary seams (turn via `cut_generation`, plan-step via
`record_plan_step_completed`), the latest generation equals the live snapshot,
and `reconstruct(head)` from the store reproduces the live snapshot (the
invariant crash recovery relies on). Absent a store, cuts are a no-op (no
behavior change to the single ``snapshot.json`` path).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.chat.services.snapshot_journal import SnapshotJournal
from reyn.events.snapshot_generations import SnapshotGenerationStore, reconstruct
from reyn.events.state_log import StateLog

AGENT = "alpha"


def _journal(tmp_path: Path, *, with_store: bool = True):
    log = StateLog(tmp_path / "state.wal")
    store = (
        SnapshotGenerationStore(AGENT, tmp_path / "generations")
        if with_store else None
    )
    journal = SnapshotJournal(
        agent_name=AGENT,
        snapshot_path=tmp_path / "snapshot.json",
        state_log=log,
        generation_store=store,
    )
    return log, store, journal


@pytest.mark.asyncio
async def test_cut_generation_records_current_snapshot(tmp_path):
    """Tier 2: cut_generation() records exactly the current snapshot as a gen."""
    log, store, journal = _journal(tmp_path)
    await journal.append_inbox(kind="user", payload={"text": "hi"})
    assert store.seqs() == []          # state change alone does not cut
    journal.cut_generation()           # turn boundary
    seq = journal.snapshot.applied_seq
    assert store.seqs() == [seq]
    assert store.load(seq) == journal.snapshot


@pytest.mark.asyncio
async def test_plan_step_boundary_cuts_generation(tmp_path):
    """Tier 2: record_plan_step_completed cuts a generation at the step boundary."""
    log, store, journal = _journal(tmp_path)
    await journal.record_plan_step_completed(
        plan_id="p1", step_id="s1", content_len=5,
    )
    assert journal.snapshot.applied_seq in store.seqs()


@pytest.mark.asyncio
async def test_reconstruct_head_equals_live_snapshot(tmp_path):
    """Tier 2: reconstruct(head) from the store == the live snapshot (parity).

    A generation is cut, then more state lands past it; reconstructing head must
    fold the post-generation WAL delta back to exactly the live snapshot — the
    crash-recovery invariant (reconstruct(head)).
    """
    log, store, journal = _journal(tmp_path)
    await journal.append_inbox(kind="user", payload={"text": "a"})
    journal.cut_generation()
    await journal.append_inbox(kind="user", payload={"text": "b"})  # past the gen
    rebuilt = reconstruct(AGENT, store, log, log.current_seq)
    assert rebuilt == journal.snapshot


@pytest.mark.asyncio
async def test_no_store_is_noop_no_behavior_change(tmp_path):
    """Tier 2: without a generation store, cut_generation is a no-op.

    The single ``snapshot.json`` path is unchanged (no behavior change) — this
    is the tests / non-chat configuration.
    """
    log, store, journal = _journal(tmp_path, with_store=False)
    assert store is None
    await journal.append_inbox(kind="user", payload={"text": "x"})
    journal.cut_generation()  # must not raise
    assert (tmp_path / "snapshot.json").is_file()
