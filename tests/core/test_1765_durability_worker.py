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


@pytest.mark.asyncio
async def test_flush_drains_inline_when_the_drainer_was_cancelled_before_it_ever_ran():
    """Tier 2: #6260 -- ``flush()`` must not hang forever when the background drainer task
    was cancelled by something OUTSIDE the worker (production reach: ``AgentRegistry.
    shutdown``'s hard-cancel of child tasks, or ``asyncio.run``'s own teardown cancelling
    every remaining task on ``/quit``/Ctrl-C) while writes were still queued. Pre-fix this
    hung: ``queue.join()`` waits on ``task_done()`` calls a dead drainer will never make, and
    ``flush()``'s own ``_kick()`` only ran BEFORE ``join()`` started waiting -- it could not
    re-kick a drainer that died only AFTER that point (this test cancels it before ``flush()``
    is even called, the simplest instance of that same class).

    No sleep, no timeout, no retry-loop -- the drainer is cancelled BEFORE it ever gets to run
    at all: no ``await`` happens between creating it (``submit_nowait``'s own ``_kick``) and
    cancelling it, so asyncio's own cooperative scheduling guarantees DETERMINISTICALLY (not
    by luck) that none of its body ever executed -- the same "no await between" argument
    ``tests/runtime/test_6240_3_append_history_durability_worker.py``'s witness ① already
    uses. The drainer task is found the way a real hard-cancel would find it -- diffing
    ``asyncio.all_tasks()`` before/after enqueuing -- never by reading ``DurabilityWorker``'s
    own private ``_drainer`` field.

    Strip-falsify: temporarily reverted ``_drain_to_empty`` to the pre-#6260 body (unconditional
    ``if not queue.empty(): self._kick()`` + ``await queue.join()``, no inline-drain fallback).
    Observed: the test HUNG (no assertion text -- the whole run never returned; had to be
    killed) instead of producing a red assertion, which is itself the defect this witness
    exists to catch: ``flush()`` never returning is exactly the #6260 production hang.
    Reverted immediately after observing; confirmed GREEN again."""
    w = DurabilityWorker()
    written: "list[str]" = []

    def _mk(tag: str):
        async def _task() -> None:
            written.append(tag)
        return _task

    before = asyncio.all_tasks()
    w.submit_nowait(_mk("a"))
    w.submit_nowait(_mk("b"))
    new_tasks = asyncio.all_tasks() - before
    assert new_tasks, "at least one drainer task must have been created by _kick()"
    drainer = new_tasks.pop()
    assert not new_tasks, "_kick() must not have created more than one drainer task"
    drainer.cancel()  # external hard-cancel -- before it ever ran

    await w.flush()

    assert written == ["a", "b"], (
        "flush() must still drain every write queued before the drainer died, even "
        f"though NO background task was left alive to do it -- got {written!r}"
    )
    await w.aclose()
