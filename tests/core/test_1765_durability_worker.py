"""Tier 2: #1765 Step 1a — the substrate-agnostic DurabilityWorker.

The worker's contract: run submitted durable-write tasks SERIALLY in FIFO order (enqueue
order = durability order — the cross-substrate ordering point), AWAIT each (blocking — the
caller's durability contract is unchanged), surface task failures to the submitter, keep the
event loop FREE while a task's off-loop fsync runs, and drain cleanly on aclose.

Real instances (no mocks): plain async callables are the injected write tasks.
"""
from __future__ import annotations

import asyncio
import logging

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


def test_rebind_carries_over_a_fire_and_forget_write_across_asyncio_run_calls():
    """Tier 2: #6261 -- a SECOND, separate ``asyncio.run()`` call against the SAME worker
    rebinds its queue to the new loop (``_ensure_queue``). Before the fix this DISCARDED
    whatever the first loop's queue still held; the architect ruling (#6261, same class of
    defect as ``events.py``'s #4966 -- a stale reference to a dead loop, there a consumer
    ``Task``, here a queue) says CARRY IT FORWARD instead. This is a plain (non-``asyncio``)
    test function -- it drives the worker through TWO independent ``asyncio.run()`` calls
    itself, the exact shape the defect needs.

    No sleep, no timeout: ``_first_loop`` finds the drainer ``_kick()`` just created
    (diffing ``asyncio.all_tasks()`` before/after, never reading ``DurabilityWorker``'s own
    private ``_drainer`` field -- the SAME technique
    ``test_flush_drains_inline_when_the_drainer_was_cancelled_before_it_ever_ran`` above uses)
    and cancels it with NO ``await`` in between -- so, deterministically (not by luck),
    NONE of the drainer's body ever executes: Python throws ``CancelledError`` into a task at
    its own very first step when ``.cancel()`` was called before that step ever ran, which
    never executes any of the coroutine's own statements. (Without this, ``asyncio.run``'s own
    teardown still gives a freshly-created, never-cancelled drainer ONE extra iteration before
    closing the loop -- confirmed empirically: a version of this test with no explicit cancel
    let the drainer run to completion inside that extra iteration, so ``written`` was
    ``["a"]`` before the sanity assertion below could ever fire red.)"""
    w = DurabilityWorker()
    written: "list[str]" = []

    def _mk(tag: str):
        async def _task() -> None:
            written.append(tag)
        return _task

    async def _first_loop() -> None:
        before = asyncio.all_tasks()
        w.submit_nowait(_mk("a"))
        new_tasks = asyncio.all_tasks() - before
        assert new_tasks, "sanity: submit_nowait's own _kick() must create a drainer"
        drainer = new_tasks.pop()
        assert not new_tasks, "sanity: _kick() must not create more than one drainer"
        drainer.cancel()  # before the loop ever gives it a turn

    asyncio.run(_first_loop())
    assert written == [], "sanity: the first loop must close before its drainer ever ran"

    async def _second_loop() -> None:
        w.submit_nowait(_mk("b"))  # triggers `_ensure_queue`'s rebind -- must carry "a" over
        await w.flush()

    asyncio.run(_second_loop())
    assert written == ["a", "b"], (
        "the write queued on the FIRST (now-dead) loop must survive the rebind and run on "
        f"the second loop, in its ORIGINAL FIFO position -- got {written!r}"
    )


def test_rebind_drops_an_awaited_write_still_queued_on_a_dead_loop_and_logs_it(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Tier 2: #6261 -- an item enqueued via the BLOCKING ``submit()`` (it carries a
    ``future``) that is still sitting in the queue when its OWN loop dies cannot be carried
    forward: the future was created on that dead loop, and its only awaiter is a coroutine
    running there too -- nobody is left to ever observe it resolve. The architect ruling: drop
    it, but NEVER silently -- the drop must be logged.

    Determinism: ``_capture``'s FIRST step runs ``submit()``'s synchronous prefix (enqueue +
    ``_kick``, creating the drainer) and then suspends on its own ``await fut`` -- exactly one
    ``await asyncio.sleep(0)`` in ``_first_loop`` gives it that one step. The drainer
    ``_kick()`` just created is then found (diffing ``asyncio.all_tasks()``, same technique as
    this file's other witnesses) and cancelled with NO ``await`` in between, so it
    deterministically never runs any of its own body (same argument as this file's other
    rebind witness above). No sleep of nonzero duration, no timeout, no retry-loop."""
    w = DurabilityWorker()
    ran = False

    async def _dropped_write() -> None:
        nonlocal ran
        ran = True

    async def _capture() -> None:
        with pytest.raises(asyncio.CancelledError):
            await w.submit(_dropped_write)

    async def _first_loop() -> None:
        before = asyncio.all_tasks()
        task = asyncio.ensure_future(_capture())
        await asyncio.sleep(0)  # exactly one turn: `_capture` enqueues + kicks + suspends
        new_tasks = asyncio.all_tasks() - before - {task}
        assert new_tasks, "sanity: submit()'s own _kick() must create a drainer"
        drainer = new_tasks.pop()
        assert not new_tasks, "sanity: _kick() must not create more than one drainer"
        drainer.cancel()  # before the loop ever gives it a turn
        assert not task.done(), "sanity: `_capture` must still be suspended on `await fut`"

    asyncio.run(_first_loop())
    assert ran is False, "sanity: the to-be-dropped write must never have actually run"

    written: "list[str]" = []

    def _mk(tag: str):
        async def _task() -> None:
            written.append(tag)
        return _task

    with caplog.at_level(logging.WARNING, logger="reyn.core.events.durability_worker"):
        async def _second_loop() -> None:
            w.submit_nowait(_mk("b"))  # triggers the rebind
            await w.flush()

        asyncio.run(_second_loop())

    assert ran is False, "the awaited write's future is gone with its loop -- it must not run"
    assert written == ["b"], f"the surviving write must still run normally -- got {written!r}"
    messages = [r.message for r in caplog.records]
    assert any("dropped 1 awaited write" in m for m in messages), (
        f"the drop must be LOGGED, never silent -- got {messages!r}"
    )


def test_rebind_drops_a_stale_end_of_burst_job_without_carrying_it_as_a_write():
    """Tier 2: #6261 -- ``on_drain_end`` (#6240 (2)) is enqueued as an ordinary ``(callable,
    None)`` item at the true end of a burst (see ``_drain``'s own docstring). If THAT item is
    still queued when its loop dies, it is a boundary SIGNAL for a burst that no longer
    exists, not durable data -- carrying it forward would just fsync twice (the new loop's
    own NEXT burst already gets its own end-of-burst job naturally); the ruling drops it
    without counting it as a write, distinctly from an ordinary fire-and-forget carry-over.

    Enqueues the EXACT SAME callable object this test already holds from construction (never
    reaching into ``DurabilityWorker``'s own private ``_on_drain_end`` attribute) -- modelling
    a stale end-of-burst job sitting in the queue without needing a real burst boundary to
    race one. Same diff-and-cancel-before-any-turn determinism as this file's other rebind
    witnesses above.

    A REAL write ("a") is queued in the SAME first burst, ahead of the stale job -- without
    it, this witness cannot tell "the terminal job was recognised and specifically dropped"
    apart from "the whole stale queue was discarded" (the pre-#6261 bug): both shapes leave
    ``calls`` holding only the SECOND loop's own natural ``"end"``. Carrying "a" forward is
    what the OTHER rebind witness above already covers; asserting it here TOO is what makes
    this test fail if the fix regressed to a blanket discard. Confirmed empirically: reverting
    ``_ensure_queue`` to its pre-#6261 body (discard the stale queue outright) turns this red
    (`assert calls == ['a', 'end']` sees `['end']` -- 'a' silently lost) where it stayed GREEN
    before this write was added."""
    calls: "list[str]" = []

    def _mk(tag: str):
        async def _task() -> None:
            calls.append(tag)
        return _task

    async def _on_drain_end() -> None:
        calls.append("end")

    w = DurabilityWorker(on_drain_end=_on_drain_end)

    async def _first_loop() -> None:
        before = asyncio.all_tasks()
        w.submit_nowait(_mk("a"))
        w.submit_nowait(_on_drain_end)
        new_tasks = asyncio.all_tasks() - before
        assert new_tasks, "sanity: submit_nowait's own _kick() must create a drainer"
        drainer = new_tasks.pop()
        assert not new_tasks, "sanity: _kick() must not create more than one drainer"
        drainer.cancel()  # before the loop ever gives it a turn

    asyncio.run(_first_loop())
    assert calls == [], "sanity: neither item must have run on the first loop"

    async def _second_loop() -> None:
        async def _noop() -> None:
            return None
        w.submit_nowait(_noop)  # triggers the rebind
        await w.flush()

    asyncio.run(_second_loop())
    assert calls == ["a", "end"], (
        "the real write ('a') must be CARRIED forward, but the STALE end-of-burst job must "
        "be DROPPED on rebind (never carried forward as an ordinary write, which would run "
        "it a SECOND time) -- the single 'end' expected here is the SECOND loop's own NEW "
        f"end-of-burst job, naturally enqueued at ITS OWN true end -- got {calls!r}"
    )
