"""Tier 2: #1765 Step 1a — the substrate-agnostic DurabilityWorker.

The worker's contract: run submitted durable-write tasks SERIALLY in FIFO order (enqueue
order = durability order — the cross-substrate ordering point), AWAIT each (blocking — the
caller's durability contract is unchanged), surface task failures to the submitter, keep the
event loop FREE while a task's off-loop fsync runs, and drain cleanly on aclose.

Real instances (no mocks): plain async callables are the injected write tasks.
"""
from __future__ import annotations

import asyncio

import pytest

from reyn.core.events.durability_worker import DurabilityWorker


@pytest.mark.asyncio
async def test_serial_fifo_order():
    """Tier 2: tasks complete in submit order (FIFO = durability order). RED if the worker
    ran tasks concurrently / out of order (the ordering the write-ahead + Step-2 build on)."""
    w = DurabilityWorker()
    done: list[int] = []

    def _mk(n: int):
        async def _task() -> None:
            await asyncio.sleep(0)  # a yield — would let a concurrent runner reorder
            done.append(n)
        return _task

    await asyncio.gather(*(w.submit(_mk(n)) for n in range(5)))
    assert done == [0, 1, 2, 3, 4], "tasks must run serially in submit (FIFO) order"
    await w.aclose()


@pytest.mark.asyncio
async def test_submit_awaits_completion_blocking():
    """Tier 2: submit returns only AFTER the task ran (blocking durability — Step 1a has no
    relaxed-durability window). RED if submit returns before the task completes."""
    w = DurabilityWorker()
    ran = False

    async def _task() -> None:
        nonlocal ran
        await asyncio.sleep(0.01)
        ran = True

    await w.submit(_task)
    assert ran is True, "submit must not return until the durable write completed"
    await w.aclose()


@pytest.mark.asyncio
async def test_loop_free_during_slow_task():
    """Tier 2: the event loop stays FREE while a slow (off-loop fsync-like) task runs — a
    concurrent ticker keeps advancing. RED if the task blocked the loop (a sync fsync would).
    """
    w = DurabilityWorker()
    ticks = 0

    async def _ticker() -> None:
        nonlocal ticks
        for _ in range(20):
            await asyncio.sleep(0.005)
            ticks += 1

    async def _slow_write() -> None:
        # mimics `await to_thread(os.fsync)` — an off-loop wait that yields the loop
        await asyncio.to_thread(__import__("time").sleep, 0.1)

    ticker = asyncio.create_task(_ticker())
    await w.submit(_slow_write)
    assert ticks > 0, "the loop must keep running (ticker advanced) during the slow off-loop write"
    await ticker
    await w.aclose()


@pytest.mark.asyncio
async def test_task_failure_surfaces_to_submitter():
    """Tier 2: a failure inside the task is re-raised by submit (same as an inline write would),
    and the worker keeps serving the next task. RED if the worker swallowed the error."""
    w = DurabilityWorker()

    async def _boom() -> None:
        raise RuntimeError("disk full")

    with pytest.raises(RuntimeError, match="disk full"):
        await w.submit(_boom)

    ran = False

    async def _ok() -> None:
        nonlocal ran
        ran = True

    await w.submit(_ok)  # the worker survived the prior failure
    assert ran is True
    await w.aclose()
