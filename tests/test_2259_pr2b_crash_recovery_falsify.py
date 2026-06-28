"""Tier 2: #2259 PR-2b item 7 — recover-to-last-durable consistent-prefix (the truncate-falsify).

The async-decoupled model writes the WAL entry, then (FIFO-after) its snapshot. A crash can land:
  (a) AFTER WAL_N is durable, BEFORE its snapshot → recovery REPLAYS WAL_N onto the prior
      snapshot = state N (this is exactly the snapshot.applied_seq ≤ durable-WAL-seq lag, resolved
      on recovery — the FIFO-lag is benign, never a hole);
  (b) BEFORE WAL_N is durable → the un-durable tail is cleanly LOST = state N-1.
Both land on a CONSISTENT PREFIX — never a torn / half-applied state. This is the owner's
recover-to-last-durable model. A GENUINE crash (a real WAL+reconstruct), not a stats-only proxy
(per the CLAUDE.md #2261 recovery-feature truncate-falsify gate).

Real StateLog + SnapshotGenerationStore + SnapshotJournal + reconstruct (no mocks).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.core.events.snapshot_generations import SnapshotGenerationStore, reconstruct
from reyn.core.events.state_log import StateLog
from reyn.runtime.services.snapshot_journal import SnapshotJournal

AGENT = "alpha"


def _journal(tmp_path: Path):
    log = StateLog(tmp_path / "state.wal")
    store = SnapshotGenerationStore(AGENT, tmp_path / "generations")
    journal = SnapshotJournal(
        agent_name=AGENT, snapshot_path=tmp_path / "snapshot.json",
        state_log=log, generation_store=store,
    )
    return log, store, journal


def _crash_mid_pair_wal_only(journal) -> None:
    """Simulate a crash BETWEEN a mutation's WAL job and its snapshot job: the in-memory mutation
    + the WAL append land, but the paired snapshot save never runs. (A WAL-only append — the
    snapshot/generation stay at the prior seq.)"""
    journal.snapshot.inbox.append({"id": "b", "kind": "user", "payload": {"text": "b"}})
    journal._wal_append_nowait(
        "inbox_put", target=AGENT, msg_id="b", msg_kind="user", payload={"text": "b"},
    )


@pytest.mark.asyncio
async def test_crash_between_wal_and_snapshot_replays_to_state_n(tmp_path):
    """Tier 2: crash AFTER WAL_N durable, BEFORE snap_N → recovery replays WAL_N onto snap_{N-1}
    = state N. RED if reconstruct lost WAL_N (snapshot ahead = a hole) or double-applied it."""
    log, store, journal = _journal(tmp_path)
    # mutation 1: full durable pair + a generation cut → gen@1 has inbox=[a].
    await journal.append_inbox(kind="user", payload={"text": "a"})
    await journal.cut_generation()
    await journal.flush()  # WAL_1 + snap_1 + gen_1 all durable

    _crash_mid_pair_wal_only(journal)
    await journal.flush()  # WAL_2 becomes durable; the snapshot/generation are still at seq 1

    # recovery: reconstruct from the durable gen (seq 1) + replay the durable WAL in (1, head].
    rebuilt = reconstruct(AGENT, store, log, log.last_durable_seq)
    texts = [m["payload"].get("text") for m in rebuilt.inbox]
    assert texts == ["a", "b"], (
        "crash mid-pair: WAL_2 (durable) must replay onto snap_1 → state 2 (consistent prefix); "
        f"got {texts}"
    )


@pytest.mark.asyncio
async def test_crash_before_wal_durable_loses_the_undurable_tail(tmp_path):
    """Tier 2: crash BEFORE WAL_N is durable → mutation N is cleanly LOST (recover-to-last-durable)
    = state N-1, a consistent prefix. RED if a non-durable mutation survived recovery."""
    log, store, journal = _journal(tmp_path)
    await journal.append_inbox(kind="user", payload={"text": "a"})
    await journal.cut_generation()
    await journal.flush()  # state 1 durable

    _crash_mid_pair_wal_only(journal)
    # NO flush — the WAL_2 job never drains (the crash). The durable WAL stays at seq 1. There is
    # no await between the submit and the reconstruct, so the drainer cannot sneak the write in.

    rebuilt = reconstruct(AGENT, store, log, log.last_durable_seq)
    texts = [m["payload"].get("text") for m in rebuilt.inbox]
    assert texts == ["a"], (
        "crash before durable: the un-durable mutation 2 must be LOST = state 1 (consistent "
        f"prefix, recover-to-last-durable); got {texts}"
    )
    await journal.flush()  # clean up the pending job


@pytest.mark.asyncio
async def test_durable_snapshot_never_leads_the_wal(tmp_path):
    """Tier 2: the FIFO-lag invariant — at every drained point, the snapshot's applied_seq is
    ≤ the durable WAL head (the snapshot never records a seq past a not-yet-durable WAL entry).
    RED if save_nowait stamped applied_seq before its WAL entry was durable."""
    log, store, journal = _journal(tmp_path)
    for i in range(5):
        await journal.append_inbox(kind="user", payload={"n": i})
        await journal.flush()
        # after each drained mutation: the snapshot's applied_seq is durable in the WAL.
        assert journal.snapshot.applied_seq <= log.last_durable_seq, (
            f"snapshot.applied_seq={journal.snapshot.applied_seq} leads durable WAL "
            f"head={log.last_durable_seq}"
        )
