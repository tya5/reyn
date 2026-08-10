"""Tier 2: #2259 PR-2b — StateLog.append_nowait (seq-in-worker / async-durability / no-lock).

The async-decoupled contract: the seq is assigned IN the worker (the WAL job, serial → monotonic
= durability order), NEVER on the task loop — so no consumer can key a durable artifact at a
not-yet-durable seq (the hole the sync-seq had; the owner caught it). append_nowait is no-return
+ non-blocking (the hot path never awaits durability). The durable watermark (`last_durable_seq`)
advances only after fsync; `last_assigned_seq` is set by the WAL job (the paired snapshot job
reads it). No `asyncio.Lock`: the worker is the single serial venue.

Real StateLog + DurabilityWorker (no mocks).
"""
from __future__ import annotations

import pytest

from reyn.core.events.state_log import StateLog


@pytest.mark.asyncio
async def test_append_nowait_is_non_blocking_and_seq_in_worker(tmp_path):
    """Tier 2: append_nowait returns immediately (no-return, non-blocking) and the seq is
    assigned IN the worker — so `last_durable_seq` lags until the write drains. RED if
    append_nowait blocked on durability (last_durable_seq would already be advanced)."""
    sl = StateLog(tmp_path / "wal.jsonl")
    assert sl.current_seq == 0 and sl.last_durable_seq == 0

    ret = sl.append_nowait("inbox_put", n=1)
    assert ret is None, "append_nowait is no-return (the seq lives in the worker)"
    # durability is DEFERRED — the WAL job has not drained (we have not awaited), so the watermark
    # has not advanced: append_nowait did not block on the write (the non-blocking win).
    assert sl.last_durable_seq == 0, "the durable write is deferred (append_nowait non-blocking)"

    await sl.aclose()  # drain the fire-and-forget write
    assert sl.last_durable_seq == 1, "after draining, the write is durable (watermark advanced)"
    assert sl.last_assigned_seq == 1, "the WAL job assigned the seq in the worker"


@pytest.mark.asyncio
async def test_sequential_append_nowait_seqs_monotonic_durable_in_order(tmp_path):
    """Tier 2: the worker assigns seqs serially (FIFO) → strictly monotonic, gap-free, and the
    durable order = submit order = seq order. RED if the worker assigned out of order or raced."""
    sl = StateLog(tmp_path / "wal.jsonl")
    for i in range(10):
        sl.append_nowait("inbox_put", n=i)

    await sl.aclose()
    assert sl.last_durable_seq == 10
    durable = [e["seq"] for e in sl.iter_from(1)]
    assert durable == list(range(1, 11)), "durable order = submit order = seq order (worker FIFO)"


@pytest.mark.asyncio
async def test_append_blocking_still_durable_on_return_with_worker_assigned_seq(tmp_path):
    """Tier 2: the blocking `append` (kept for callers that need durable-on-return) returns the
    WORKER-assigned seq via a per-call holder, and only AFTER its write is durable. Guards that
    the seq-in-worker refactor didn't break the blocking contract."""
    sl = StateLog(tmp_path / "wal.jsonl")
    seq = await sl.append("inbox_put", n=1)
    assert seq == 1, "blocking append returns the worker-assigned seq"
    assert sl.last_durable_seq == 1, "blocking append returns only AFTER its write is durable"
    await sl.aclose()
