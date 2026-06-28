"""Tier 2: #2259 PR-2b — StateLog.append_nowait (sync-seq / async-durability / no-lock).

The async-decoupled contract: the seq COUNTER is assigned SYNCHRONOUSLY (so `current_seq` is
correct immediately — the 7 synchronous consumers, e.g. config-gen keying, must not see a stale
head), while only the WAL WRITE is deferred off-loop (fire-and-forget via the worker). No
`asyncio.Lock`: the worker is the single serial venue, so the sync-atomic seq increment never
races and durable-order = submit-order = seq-order (FIFO). The durable watermark
(`last_durable_seq`) advances only after fsync — so a reader/truncate never outruns durability.

Real StateLog + DurabilityWorker (no mocks).
"""
from __future__ import annotations

import pytest

from reyn.core.events.state_log import StateLog


@pytest.mark.asyncio
async def test_append_nowait_is_sync_seq_async_durability(tmp_path):
    """Tier 2: append_nowait assigns the seq SYNCHRONOUSLY (current_seq advances immediately)
    but DEFERS the durable write (last_durable_seq lags until drained). RED if append_nowait
    blocked on durability (last_durable_seq would already be 1) or current_seq lagged."""
    sl = StateLog(tmp_path / "wal.jsonl")
    assert sl.current_seq == 0 and sl.last_durable_seq == 0

    seq = sl.append_nowait("inbox_put", n=1)
    # seq + current_seq are correct SYNCHRONOUSLY (no await happened yet).
    assert seq == 1
    assert sl.current_seq == 1, "the seq is assigned synchronously (current_seq is immediate)"
    assert sl.last_assigned_seq == 1
    # durability is DEFERRED — the drainer has not run (we have not awaited), so the watermark
    # has not advanced: append_nowait did not block on the write (the non-blocking win).
    assert sl.last_durable_seq == 0, "the durable write is deferred (append_nowait non-blocking)"

    await sl.aclose()  # drain the fire-and-forget write
    assert sl.last_durable_seq == 1, "after draining, the write is durable (watermark advanced)"


@pytest.mark.asyncio
async def test_concurrent_append_nowait_seqs_monotonic_without_lock(tmp_path):
    """Tier 2: synchronous seq assignment (no lock) yields strictly monotonic, gap-free seqs,
    and the durable order = seq order (worker FIFO). RED if the counter raced (dup/gap) or the
    writes landed out of order."""
    sl = StateLog(tmp_path / "wal.jsonl")
    seqs = [sl.append_nowait("inbox_put", n=i) for i in range(10)]
    assert seqs == list(range(1, 11)), "sync-atomic assignment → monotonic, gap-free, no lock"

    await sl.aclose()
    assert sl.last_durable_seq == 10
    durable = [e["seq"] for e in sl.iter_from(1)]
    assert durable == list(range(1, 11)), "durable order = submit order = seq order (FIFO)"


@pytest.mark.asyncio
async def test_append_blocking_still_durable_on_return(tmp_path):
    """Tier 2: the blocking `append` (kept for callers that need durable-on-return) still awaits
    durability — last_durable_seq has advanced by the time it returns. Guards that the sync-seq
    refactor didn't accidentally make `append` non-blocking too."""
    sl = StateLog(tmp_path / "wal.jsonl")
    seq = await sl.append("inbox_put", n=1)
    assert seq == 1
    assert sl.last_durable_seq == 1, "blocking append returns only AFTER its write is durable"
    await sl.aclose()
