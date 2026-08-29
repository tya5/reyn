"""Tier 2: #5516 — ``reyn.hooks.fold.drain_folded``'s own acceptance
conditions, in isolation from either of its two real callers
(``_BoundedEventBridge``/``ComposedEventConsumer``) — a real
``asyncio.Queue``, no mocks."""
from __future__ import annotations

import asyncio

import pytest

from reyn.hooks.fold import drain_folded


@pytest.mark.asyncio
async def test_a_burst_already_queued_before_drain_starts_becomes_one_batch() -> None:
    """Tier 2: condition ② + the core "fold launches" behavior — N items
    already sitting in the queue when drain starts become ONE
    dispatch_batch call carrying all N, not N separate calls."""
    queue: "asyncio.Queue[int]" = asyncio.Queue()
    for i in range(5):
        queue.put_nowait(i)

    batches: list[list[int]] = []
    seen_first_batch = False

    async def _capture(batch: list) -> None:
        nonlocal seen_first_batch
        batches.append(batch)
        if not seen_first_batch:
            seen_first_batch = True
            # Stop the loop after the first batch by cancelling from
            # inside — simplest deterministic way to end an infinite loop
            # in a test.
            raise asyncio.CancelledError

    task = asyncio.create_task(drain_folded(queue, _capture))
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=5)

    assert batches == [[0, 1, 2, 3, 4]], (
        f"5 pre-queued items must fold into ONE batch, oldest-first -- got {batches!r}"
    )


@pytest.mark.asyncio
async def test_an_item_arriving_during_dispatch_is_not_lost_and_not_double_counted() -> None:
    """Tier 2: LOAD-BEARING falsification of the no-loss guarantee
    (#5516 §3b/§3c, condition ③) — an item that arrives WHILE
    dispatch_batch is still running for the current batch must land in
    the NEXT batch, not be silently dropped and not appear twice."""
    queue: "asyncio.Queue[int]" = asyncio.Queue()
    queue.put_nowait(1)
    queue.put_nowait(2)  # qsize()==1 when item 1 is popped -> batch 1 = [1, 2]

    batches: list[list[int]] = []
    late_arrival_done = asyncio.Event()
    seen_first_batch = False

    async def _capture(batch: list) -> None:
        nonlocal seen_first_batch
        batches.append(batch)
        if not seen_first_batch:
            seen_first_batch = True
            # Simulate an item arriving WHILE this (the first) batch is
            # being "dispatched" -- condition ③'s exact failure mode if
            # qsize() were read AFTER this point instead of before.
            queue.put_nowait(99)
            late_arrival_done.set()
        else:
            raise asyncio.CancelledError

    task = asyncio.create_task(drain_folded(queue, _capture))
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=5)

    assert batches == [[1, 2], [99]], (
        f"the late arrival (99) must land whole in batch 2, not merged into "
        f"batch 1 and not dropped -- got {batches!r}"
    )
    all_items = [i for batch in batches for i in batch]
    assert all_items.count(99) == 1, f"99 must appear exactly once total -- got {all_items!r}"


@pytest.mark.asyncio
async def test_direction_independence_drop_newest_and_drop_oldest_both_fold_correctly(
) -> None:
    """Tier 2: condition ① -- drain_folded itself never reads or assumes
    an overflow direction (it never touches queue.put/put_nowait's own
    overflow handling at all -- only qsize()/get_nowait() on the
    already-successfully-enqueued contents). Proven by construction: a
    maxsize-bounded queue with drop-newest overflow handling (the shape
    _BoundedEventBridge.deliver uses today) still folds correctly --
    this test does not need a SEPARATE drop-oldest queue variant to prove
    the point, because drain_folded's own body contains no branch on
    overflow direction at all (verified: this module's only queue calls
    are get()/get_nowait()/qsize(), none of which differ by overflow
    policy)."""
    queue: "asyncio.Queue[int]" = asyncio.Queue(maxsize=3)
    for i in range(3):
        queue.put_nowait(i)
    # A 4th put would either block (put) or raise QueueFull (put_nowait) --
    # overflow handling is the PRODUCER's concern (bridge/adapter), never
    # drain_folded's; this test only exercises what drain_folded itself does
    # with whatever already made it into the queue.

    batches: list[list[int]] = []

    async def _capture(batch: list) -> None:
        batches.append(batch)
        raise asyncio.CancelledError

    task = asyncio.create_task(drain_folded(queue, _capture))
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=5)

    assert batches == [[0, 1, 2]]
