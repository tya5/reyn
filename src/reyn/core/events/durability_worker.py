"""DurabilityWorker — a single serial worker that runs durable writes off the event loop.

#1765 Step 1a. A blocking ``os.fsync`` does not yield, so it freezes the WHOLE event loop
(TUI repaint + every concurrent session) for its duration — on slow storage that is hundreds
of ms per fsync. This worker moves the fsync OFF the loop: a substrate submits a durable-write
task (a coroutine that writes + ``await asyncio.to_thread(os.fsync, …)``), the worker runs it
serially, and ``submit`` awaits its completion — so the caller's durability contract is
UNCHANGED (it returns only once durable) while the loop stays free DURING the fsync.

#2259 PR-2a. The serial point evolved from an ``asyncio.Lock`` to a **queue + background
drainer** — the structural prerequisite for the #2259 PR-2b non-blocking (fire-and-forget)
submit — but BEHIND THE SAME ``submit``-awaits contract: ``submit`` enqueues a task + awaits a
per-task future the drainer resolves, so enqueue order = durability order (FIFO) and the caller
still returns only once durable (all callers untouched). PR-2a also adds the §4 durable-write
RETRY: a transient ``OSError`` (disk full / EIO / a momentary fs hiccup) is retried with bounded
exponential backoff; on retry-exhaustion the failure is PERSISTENT and re-raised to the submitter
(fail-stop escalation — the same "a failure is re-raised here" contract, now after bounded retry).

Substrate-agnostic by construction (P7): the worker holds no WAL / snapshot / workspace
knowledge — the injected write callable carries all of it (including any post-write bookkeeping
such as a durable-seq watermark). Its one structural guarantee is **serial FIFO**: tasks run
one at a time in submit order, so *enqueue order = durability order*. That single ordering point
is what later steps build on — the cross-substrate write-ahead ordering (a depended-upon
substrate submitted before the WAL event that references it) and, in #2259 PR-2b, non-blocking
writes (``submit`` returns before the task runs, with a barrier awaiting only where an external
effect is gated). For PR-2a ``submit`` always awaits (no relaxed-durability window).
"""
from __future__ import annotations

import asyncio
from typing import Awaitable, Callable

from reyn.core.retry import backoff_s

DurableWrite = Callable[[], Awaitable[None]]

# §4 durable-write retry bounds (standard bounded exponential backoff; short — a durable write
# is local I/O, not a network call). Persistent failure past these attempts = fail-stop escalate.
_WRITE_RETRY_BASE_S = 0.05
_WRITE_RETRY_MAX_S = 2.0
_WRITE_MAX_ATTEMPTS = 5


class DurabilityWorker:
    """A single serialisation point for off-loop durable writes (see module docstring).

    The serial point is a queue drained by ONE background task: ``submit`` enqueues ``(task,
    future)`` and awaits the future (FIFO — the drainer runs tasks in enqueue order, so submit
    order = durability order), and the task's ``await to_thread(os.fsync)`` keeps the loop free;
    other submits wait their turn. The queue + drainer are (re)bound lazily to the running loop
    on first submit, so a default-constructed worker holds nothing until used and survives a
    fresh event loop (tests / re-init) without leaking a stale task. #2259 PR-2b flips ``submit``
    to non-blocking behind this SAME structure — so the substrate routing built on it is not
    throwaway."""

    def __init__(
        self, *, max_write_attempts: int = _WRITE_MAX_ATTEMPTS,
        retry_base_s: float = _WRITE_RETRY_BASE_S, retry_max_s: float = _WRITE_RETRY_MAX_S,
    ) -> None:
        self._max_write_attempts = max_write_attempts
        self._retry_base_s = retry_base_s
        self._retry_max_s = retry_max_s
        self._queue: "asyncio.Queue | None" = None
        self._drainer: "asyncio.Task | None" = None
        self._loop: "asyncio.AbstractEventLoop | None" = None
        # #2259 PR-2b: a fire-and-forget (non-blocking) durable write that fails PERSISTENTLY
        # (§4 retry-exhausted) has no submitter to re-raise to, so its escalation is a
        # health-signal: this latches True + a CRITICAL log. The system is no longer durably
        # persisting — a supervisor reads `durability_failed` to fail-stop. Never auto-cleared.
        self._durability_failed = False

    def _ensure_queue(self) -> "asyncio.Queue":
        """Bind (or rebind) the queue to the RUNNING loop and return it. A new loop (a fresh test,
        a re-init) gets a fresh queue + resets the drainer to None (the old loop's task is
        abandoned — inert; in production there is one loop). Does NOT start the drainer — callers
        enqueue FIRST, then ``_kick``, so the (self-terminating) drainer never sees an empty queue
        before the item lands."""
        loop = asyncio.get_running_loop()
        if self._loop is not loop:
            self._queue = asyncio.Queue()
            self._loop = loop
            self._drainer = None  # old drainer (old loop) abandoned; _kick starts a fresh one
        assert self._queue is not None
        return self._queue

    def _kick(self) -> None:
        """Start the self-terminating drainer if it is not currently running. Called AFTER the
        item is enqueued, so the drainer is guaranteed to see it."""
        if self._drainer is None or self._drainer.done():
            self._drainer = self._loop.create_task(self._drain())  # type: ignore[union-attr]

    async def submit(self, do_durable_write: DurableWrite) -> None:
        """Run a durable-write task, serialised with every other submit (FIFO = durability
        order), and AWAIT it (#2259 PR-2a — blocking: no relaxed-durability window). The task's
        off-loop fsync keeps the event loop free for non-durability work; other submits wait
        their turn. A transient ``OSError`` is retried with bounded backoff (§4); on exhaustion
        the persistent failure is re-raised here (same as an inline write, now after retry)."""
        queue = self._ensure_queue()
        fut: "asyncio.Future[None]" = self._loop.create_future()  # type: ignore[union-attr]
        queue.put_nowait((do_durable_write, fut))
        self._kick()
        await fut

    def submit_nowait(self, do_durable_write: DurableWrite) -> None:
        """#2259 PR-2b: enqueue a durable write FIRE-AND-FORGET — return IMMEDIATELY, do NOT
        await durability (the in-memory mutation already happened on the task loop; this is the
        relaxed-durability window). SYNCHRONOUS enqueue (``put_nowait``), so a (WAL, snapshot)
        pair submitted back-to-back with no ``await`` between them is atomic on the event loop —
        no concurrent mutation's job can interleave between the pair (invariant: ``snap_N`` reads
        the seq ``WAL_N`` assigned, never a later one). The drainer runs it serially (FIFO =
        durability order); a persistent (§4-exhausted) failure has no submitter to raise to, so it
        latches ``durability_failed`` + CRITICAL-logs (health-signal escalation)."""
        queue = self._ensure_queue()
        queue.put_nowait((do_durable_write, None))
        self._kick()

    @property
    def durability_failed(self) -> bool:
        """True once a fire-and-forget durable write failed PERSISTENTLY (§4-exhausted). The
        system is no longer durably persisting — a supervisor fail-stops on this. Latched."""
        return self._durability_failed

    async def _drain(self) -> None:
        """The SELF-TERMINATING drainer: process queued ``(task, future)`` items in FIFO order
        (= durability order) until the queue is EMPTY, then exit. It does NOT block on a perpetual
        ``await queue.get()`` — a perpetual drainer leaks across an event-loop teardown (a test
        that never ``aclose``s it): at loop close, asyncio cancels the pending ``get()`` and its
        internal getter ``call_soon`` raises "Event loop is closed". Draining via ``get_nowait``
        and exiting on empty avoids the leak entirely.

        No-stranding: the ``QueueEmpty`` check + ``return`` are atomic (no ``await`` between), so a
        concurrent ``submit`` cannot interleave there — an item enqueued while a prior one is
        processing is seen on the next iteration; an item enqueued after the drainer exits is
        picked up when the next submit re-kicks it (``_ensure_runtime`` restarts a ``done()``
        drainer).

        ``CancelledError`` MUST propagate (terminate the drainer): it is NOT a write failure.
        Swallowing it (catching ``BaseException``) made an earlier drainer immortal — a cancel
        landing mid-write was caught + the loop continued, and ``_cancel_all_tasks`` teardown hung
        forever. So a cancel resolves the in-flight future + re-raises; only a real ``Exception``
        (a write failure) is surfaced (to the submitter, or as the health-signal)."""
        assert self._queue is not None
        while True:
            try:
                do_durable_write, fut = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                return  # drained → self-terminate (atomic with the check: no await between)
            try:
                await self._run_with_retry(do_durable_write)
            except asyncio.CancelledError:
                if fut is not None and not fut.done():
                    fut.cancel()
                self._queue.task_done()
                raise
            except Exception as e:  # noqa: BLE001 — a real write failure → surface it
                self._on_write_failure(fut, e)
                self._queue.task_done()
            else:
                if fut is not None and not fut.done():
                    fut.set_result(None)
                self._queue.task_done()

    def _on_write_failure(self, fut: "asyncio.Future | None", e: Exception) -> None:
        """Surface a persistent (§4-exhausted) durable-write failure. A BLOCKING submit (``fut``
        present) re-raises to the awaiting caller (PR-2a contract). A FIRE-AND-FORGET submit
        (``fut is None``, PR-2b) has no caller — so the escalation MUST surface out-of-band, never
        be swallowed (the owner's "no silent unbounded loss": in-memory must not race ahead while
        durability is silently dead): latch ``durability_failed`` + CRITICAL-log."""
        if fut is not None:
            if not fut.done():
                fut.set_exception(e)
            return
        self._durability_failed = True
        import logging  # noqa: PLC0415
        logging.getLogger(__name__).critical(
            "DURABILITY FAILURE (fire-and-forget, §4-exhausted): a durable write failed "
            "persistently — the system is no longer durably persisting; fail-stop required. %s",
            e,
        )

    async def _run_with_retry(self, do_durable_write: DurableWrite) -> None:
        """§4: run the durable write, retrying a TRANSIENT ``OSError`` with bounded exponential
        backoff. On retry-exhaustion the failure is PERSISTENT → re-raise (fail-stop escalation).
        A non-``OSError`` (a programming error — retrying cannot help) is raised immediately."""
        attempt = 0
        while True:
            try:
                await do_durable_write()
                return
            except OSError:
                if attempt >= self._max_write_attempts - 1:
                    raise  # persistent (retry-exhausted) → escalate to the submitter
                await asyncio.sleep(
                    backoff_s(attempt, base_s=self._retry_base_s, max_s=self._retry_max_s)
                )
                attempt += 1

    async def aclose(self) -> None:
        """Graceful shutdown. Drain every enqueued task (no in-flight write lost), then stop. The
        self-terminating drainer may already have exited, so KICK it if the queue is non-empty,
        ``join`` to wait out the drain, then cancel any still-running drainer. A no-op if never
        used, or if called on a different loop than the one the queue is bound to (a dead loop —
        nothing to drain there)."""
        if self._queue is None or self._loop is None:
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is not self._loop:
            return
        if not self._queue.empty():
            self._kick()
        await self._queue.join()
        if self._drainer is not None and not self._drainer.done():
            self._drainer.cancel()
            try:
                await self._drainer
            except asyncio.CancelledError:
                pass
