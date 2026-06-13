"""Tier 2: OS invariant — rewind reset-record + active-pointer honor (keystone).

ADR-0038 Stage 1b. Real `StateLog` + `AgentSnapshot` + `SnapshotGenerationStore`
(no mocks). rewind is an append-only compensating record; reconstruct honors the
active branch (skips abandoned segments, never bases on an abandoned-branch
generation). Covers the correctness keystone: crash-mid-rewind idempotence,
post-rewind work preservation, both nested shapes (subsuming / partial), and the
Phase-1 active-target guard.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.events.agent_snapshot import AgentSnapshot
from reyn.events.snapshot_generations import (
    RewindIntoAbandonedError,
    SnapshotGenerationStore,
    is_active_seq,
    reconstruct,
    rewind,
)
from reyn.events.state_log import StateLog

AGENT = "alpha"


async def _put(log: StateLog, text: str) -> int:
    """Append an inbox_put for AGENT (msg_id == text for easy assertion)."""
    return await log.append(
        "inbox_put", target=AGENT, msg_id=text, msg_kind="user",
        payload={"text": text},
    )


def _inbox_ids(snap: AgentSnapshot) -> list[str]:
    return [m["id"] for m in snap.inbox]


def _empty_store(tmp_path: Path) -> SnapshotGenerationStore:
    return SnapshotGenerationStore(AGENT, tmp_path / "generations")


@pytest.mark.asyncio
async def test_reconstruct_skips_abandoned_after_rewind(tmp_path):
    """Tier 2: a rewind abandons the undone future; reconstruct(head) skips it."""
    log = StateLog(tmp_path / "wal")
    await _put(log, "a")          # seq 1
    await _put(log, "b")          # seq 2
    await _put(log, "c")          # seq 3
    await rewind(log, target_n=1)  # seq 4 — undo back to 1 (abandons 2,3)
    assert is_active_seq(log, 1) and not is_active_seq(log, 2) and not is_active_seq(log, 3)
    snap = reconstruct(AGENT, _empty_store(tmp_path), log, log.current_seq)
    assert _inbox_ids(snap) == ["a"]   # b, c undone


@pytest.mark.asyncio
async def test_post_rewind_work_yields_current_state(tmp_path):
    """Tier 2: reconstruct(head) keeps work done AFTER the rewind (refinement).

    reconstruct(active-pointer-target) would lose 'd' — reconstruct(head)+is_active
    keeps it (active = ≤N ∪ (R, head]).
    """
    log = StateLog(tmp_path / "wal")
    await _put(log, "a")          # seq 1
    await _put(log, "b")          # seq 2
    await rewind(log, target_n=1)  # seq 3 — abandons 2
    await _put(log, "d")          # seq 4 — new work on the active branch
    snap = reconstruct(AGENT, _empty_store(tmp_path), log, log.current_seq)
    assert _inbox_ids(snap) == ["a", "d"]   # b undone, d kept


@pytest.mark.asyncio
async def test_crash_mid_rewind_idempotent(tmp_path):
    """Tier 2: crash mid-rewind ⇒ restore yields as-of-N, idempotently (keystone).

    Reset-record is fsync'd before reconstruction; with head == the rewind record
    (no new work yet), reconstruct(head) collapses to as-of-N and never resurrects
    the abandoned future — twice over (idempotent).
    """
    log = StateLog(tmp_path / "wal")
    await _put(log, "a")          # seq 1
    await _put(log, "b")          # seq 2
    await rewind(log, target_n=1)  # seq 3 = head; crash here, no new work
    first = reconstruct(AGENT, _empty_store(tmp_path), log, log.current_seq)
    second = reconstruct(AGENT, _empty_store(tmp_path), log, log.current_seq)
    assert _inbox_ids(first) == ["a"]          # b (abandoned future) not resurrected
    assert first == second                     # idempotent


@pytest.mark.asyncio
async def test_nested_rewind_subsuming(tmp_path):
    """Tier 2: a 2nd rewind further back subsumes the 1st (nested shape a)."""
    log = StateLog(tmp_path / "wal")
    await _put(log, "a")           # seq 1
    await _put(log, "b")           # seq 2
    await rewind(log, target_n=1)  # seq 3 — abandons 2
    await _put(log, "c")           # seq 4
    await rewind(log, target_n=1)  # seq 5 — back to 1 again, subsumes the 1st rewind
    snap = reconstruct(AGENT, _empty_store(tmp_path), log, log.current_seq)
    assert _inbox_ids(snap) == ["a"]   # b and c both undone


@pytest.mark.asyncio
async def test_nested_rewind_partial(tmp_path):
    """Tier 2: a 2nd rewind on the new branch does NOT subsume (nested shape b).

    Exercises the non-trivial 'skip rewinds on abandoned branches' path NOT
    firing for the 1st rewind (it stays active), so both abandoned segments hold.
    """
    log = StateLog(tmp_path / "wal")
    await _put(log, "a")           # seq 1
    await _put(log, "b")           # seq 2
    await rewind(log, target_n=1)  # seq 3 — abandons 2
    await _put(log, "c")           # seq 4 (active, new branch)
    await _put(log, "d")           # seq 5 (active, new branch)
    await rewind(log, target_n=4)  # seq 6 — undo back to 4 (abandons 5), keeps c
    snap = reconstruct(AGENT, _empty_store(tmp_path), log, log.current_seq)
    assert _inbox_ids(snap) == ["a", "c"]   # b abandoned (1st), d abandoned (2nd)


@pytest.mark.asyncio
async def test_rewind_to_abandoned_seq_rejected(tmp_path):
    """Tier 2: rewinding into an abandoned segment is rejected (Phase-1 guard)."""
    log = StateLog(tmp_path / "wal")
    await _put(log, "a")           # seq 1
    await _put(log, "b")           # seq 2
    await rewind(log, target_n=1)  # seq 3 — seq 2 now abandoned
    with pytest.raises(RewindIntoAbandonedError):
        await rewind(log, target_n=2)   # seq 2 is on an abandoned branch → Phase-2 fork


@pytest.mark.asyncio
async def test_no_rewind_is_backward_compatible(tmp_path):
    """Tier 2: with no rewind records every seq is active (Stage-1a behavior)."""
    log = StateLog(tmp_path / "wal")
    await _put(log, "a")
    await _put(log, "b")
    assert is_active_seq(log, 1) and is_active_seq(log, 2)
    snap = reconstruct(AGENT, _empty_store(tmp_path), log, log.current_seq)
    assert _inbox_ids(snap) == ["a", "b"]


@pytest.mark.asyncio
async def test_abandoned_branch_generation_not_used_as_base(tmp_path):
    """Tier 2: a generation cut on the abandoned branch is not used as the base.

    Reconstruct must pick the nearest ACTIVE generation; an abandoned-branch gen
    (whose seq is inside the abandoned segment) is skipped so its contaminated
    state never leaks into the active reconstruction.
    """
    log = StateLog(tmp_path / "wal")
    store = _empty_store(tmp_path)
    await _put(log, "a")           # seq 1
    await _put(log, "b")           # seq 2
    # Cut a generation on what will become the abandoned branch (applied_seq 2).
    store.record(reconstruct(AGENT, store, log, 2))
    assert store.seqs() == [2]
    await rewind(log, target_n=1)  # seq 3 — abandons 2 (incl. the gen at seq 2)
    snap = reconstruct(AGENT, store, log, log.current_seq)
    assert _inbox_ids(snap) == ["a"]   # the abandoned gen@2 (with 'b') is not the base
