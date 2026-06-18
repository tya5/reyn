"""Tier 2: #1751 — StateLog.append fsync does not block the event loop.

The TUI-latency root cause: ``os.fsync`` ran synchronously inside the async append,
so on slow storage (cloud-sync / network-FS / APFS pressure) every fsync froze the
whole event loop (TUI repaint, other sessions) for its duration. The fix runs the
fsync via ``asyncio.to_thread`` — still AWAITED (durability/recovery contract intact)
but off the loop, under the existing append lock (no WAL interleave).

This pins that a slow fsync no longer starves a concurrent coroutine. Falsification
(feedback_falsify_acceptance_test_before_proof): revert to a synchronous
``os.fsync(...)`` and the concurrent ticker stalls for the whole fsync → the
progress assertion reds.
"""
from __future__ import annotations

import asyncio
import os
import time

import pytest

from reyn.core.events.state_log import StateLog

_FSYNC_BLOCK_SECONDS = 0.2


@pytest.mark.asyncio
async def test_append_fsync_does_not_block_the_event_loop(tmp_path, monkeypatch):
    """Tier 2: a slow fsync runs off-loop — a concurrent coroutine keeps progressing."""
    log = StateLog(tmp_path / "wal.jsonl")

    # Simulate slow storage: fsync blocks (synchronously) for 200ms.
    real_fsync = os.fsync
    monkeypatch.setattr(
        os, "fsync",
        lambda fd: (time.sleep(_FSYNC_BLOCK_SECONDS), real_fsync(fd))[1],
    )

    ticks = 0

    async def _ticker() -> None:
        nonlocal ticks
        for _ in range(1000):
            ticks += 1
            await asyncio.sleep(0.001)

    ticker = asyncio.create_task(_ticker())
    try:
        # The append's fsync blocks 200ms; with the to_thread fix the loop stays
        # free, so the 1ms ticker advances many times during that window.
        await log.append(
            "inbox_put", target="a", msg_id="m1", msg_kind="user", payload={},
        )
        # Synchronous-fsync would freeze the loop for the whole 200ms → ticks ~0-1.
        # Off-loop fsync → the ticker progressed well past that.
        assert ticks >= 10, (
            f"event loop was starved during fsync (ticks={ticks}); "
            "fsync must run off the loop via asyncio.to_thread"
        )
    finally:
        ticker.cancel()

    # Durability/correctness preserved: the entry is on disk after append returns
    # (the fsync is awaited to completion, not fire-and-forget).
    seqs = [e["seq"] for e in log.iter_from(0) if e.get("kind") == "inbox_put"]
    assert seqs == [1]


@pytest.mark.asyncio
async def test_concurrent_appends_stay_serialized(tmp_path):
    """Tier 2: the lock still serializes concurrent appends (no WAL interleave) —
    the to_thread yield happens INSIDE the held lock, so seqs are unique + ordered."""
    log = StateLog(tmp_path / "wal.jsonl")

    async def _put(text: str) -> int:
        return await log.append(
            "inbox_put", target="a", msg_id=text, msg_kind="user", payload={"t": text},
        )

    seqs = await asyncio.gather(*[_put(f"m{i}") for i in range(20)])
    # Every append got a distinct, contiguous seq — no interleave / lost write.
    assert sorted(seqs) == list(range(1, 21))
    on_disk = [e["seq"] for e in log.iter_from(0) if e.get("kind") == "inbox_put"]
    assert sorted(on_disk) == list(range(1, 21))
