"""Tier 2: #1765 Step 1a — StateLog routed through the DurabilityWorker (off-loop WAL fsync).

The refactor is BEHAVIOR-INVARIANT for durability + recovery (the off-loop fsync is the only
change): an append returns only once durable, recovery replays every durable entry, and the
`_durable_seq` watermark closes the #1751 lockless-read surface (a written-but-not-yet-fsync'd
tail entry is structurally unreadable). Real instances (no mocks).
"""
from __future__ import annotations

import json

import pytest

from reyn.core.events.state_log import StateLog


@pytest.mark.asyncio
async def test_append_is_durable_then_recovery_replays_all(tmp_path):
    """Tier 2: completeness + recovery. Each append returns only after its entry is durable
    (per-append contract unchanged); after a simulated restart a FRESH StateLog on the same
    file replays EVERY entry. RED if the watermark didn't init from disk (recovery would see
    an empty log) or appends didn't actually persist."""
    p = tmp_path / "wal.jsonl"
    sl = StateLog(p)
    s1 = await sl.append("inbox_put", text="a")
    s2 = await sl.append("inbox_consume", msg_id="m")
    assert (s1, s2) == (1, 2)
    # durable on disk the instant append returned (the per-append contract):
    on_disk = [json.loads(ln)["seq"] for ln in p.read_text().splitlines() if ln.strip()]
    assert on_disk == [1, 2]
    await sl.aclose()
    # restart: a fresh StateLog inits the durable watermark from the on-disk max, so a
    # full replay returns every entry (the recovery behaviour — the public proof).
    recovered = StateLog(p)
    assert [e["seq"] for e in recovered.iter_from(1)] == [1, 2]


@pytest.mark.asyncio
async def test_iter_from_excludes_inflight_entry_during_fsync(tmp_path, monkeypatch):
    """Tier 2: the #1751 closure (the REAL concurrent scenario). While an append's entry is
    written-but-still-fsyncing (off-loop), a CONCURRENT iter_from must NOT expose it — it is
    not yet durable. After the fsync completes it becomes readable. RED if the `_inflight_seq`
    skip is removed (the concurrent read would see the non-durable entry — the surface that
    reverted #1751)."""
    import asyncio
    import os
    import time

    orig_fsync = os.fsync

    def _slow_fsync(fd):  # runs off-loop in to_thread → holds the in-flight window open
        time.sleep(0.05)
        return orig_fsync(fd)

    monkeypatch.setattr(os, "fsync", _slow_fsync)

    p = tmp_path / "wal.jsonl"
    sl = StateLog(p)
    await sl.append("inbox_put", text="durable")  # seq 1 — durable
    appending = asyncio.create_task(sl.append("inbox_put", text="inflight"))  # seq 2
    await asyncio.sleep(0.01)  # let seq 2 reach the file + enter the (slow) fsync

    during = [e["seq"] for e in sl.iter_from(1)]
    assert during == [1], "a concurrent read must exclude the in-flight (non-durable) entry seq 2"

    await appending  # fsync completes → seq 2 is now durable
    after = [e["seq"] for e in sl.iter_from(1)]
    assert after == [1, 2], "after the fsync, the now-durable entry is readable"
    await sl.aclose()


@pytest.mark.asyncio
async def test_seq_order_equals_file_order_under_concurrency(tmp_path):
    """Tier 2: concurrent appends still land in seq order on disk (the seq is assigned + the
    worker write submitted under the lock, so submit order = file order). RED if a coroutine
    could be preempted between seq-assign and submit (reordering the file)."""
    p = tmp_path / "wal.jsonl"
    sl = StateLog(p)
    import asyncio
    await asyncio.gather(*(sl.append("inbox_put", text=str(i)) for i in range(20)))
    on_disk = [json.loads(ln)["seq"] for ln in p.read_text().splitlines() if ln.strip()]
    assert on_disk == list(range(1, 21)), "file order must equal seq order"
    await sl.aclose()
