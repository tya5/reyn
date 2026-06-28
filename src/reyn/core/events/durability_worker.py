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

    def _ensure_runtime(self) -> "asyncio.Queue":
        """Bind (or rebind) the queue + drainer to the RUNNING loop. A new loop (a fresh test, a
        re-init) gets a fresh queue + drainer — the old loop is gone, so its queue/task are inert
        and dropped (in production there is one loop; any in-flight write already completed under
        ``aclose``). Idempotent within a loop: a live drainer is reused."""
        loop = asyncio.get_running_loop()
        if self._loop is not loop:
            self._queue = asyncio.Queue()
            self._loop = loop
            self._drainer = loop.create_task(self._drain())
        elif self._drainer is None or self._drainer.done():
            self._drainer = loop.create_task(self._drain())
        assert self._queue is not None
        return self._queue

    async def submit(self, do_durable_write: DurableWrite) -> None:
        """Run a durable-write task, serialised with every other submit (FIFO = durability
        order), and AWAIT it (#2259 PR-2a — blocking: no relaxed-durability window). The task's
        off-loop fsync keeps the event loop free for non-durability work; other submits wait
        their turn. A transient ``OSError`` is retried with bounded backoff (§4); on exhaustion
        the persistent failure is re-raised here (same as an inline write, now after retry)."""
        queue = self._ensure_runtime()
        fut: "asyncio.Future[None]" = self._loop.create_future()  # type: ignore[union-attr]
        queue.put_nowait((do_durable_write, fut))
        await fut

    async def _drain(self) -> None:
        """The single background drainer: process ``(task, future)`` items one at a time in
        enqueue order (FIFO = durability order), resolving each future with the task's result or
        its (post-retry) failure. Runs until cancelled (``aclose`` / event-loop teardown).

        ``CancelledError`` MUST propagate (terminate the drainer) — it is NOT a write failure.
        Swallowing it (catching ``BaseException``) made the drainer immortal: a cancel landing
        mid-write was caught, the loop continued to an empty ``get()``, and ``asyncio.run``'s
        ``_cancel_all_tasks`` teardown then hung forever waiting for the task to finish. So a
        cancel resolves the in-flight future (the submitter is unblocked) and re-raises; only a
        real ``Exception`` (a write failure) is surfaced to the submitter."""
        assert self._queue is not None
        while True:
            do_durable_write, fut = await self._queue.get()
            try:
                await self._run_with_retry(do_durable_write)
            except asyncio.CancelledError:
                if not fut.done():
                    fut.cancel()
                self._queue.task_done()
                raise
            except Exception as e:  # noqa: BLE001 — a real write failure → surface to the submitter
                if not fut.done():
                    fut.set_exception(e)
                self._queue.task_done()
            else:
                if not fut.done():
                    fut.set_result(None)
                self._queue.task_done()

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
        """Graceful shutdown. Drain every enqueued task (so no in-flight write is lost), then
        cancel the idle drainer. A no-op if never used (no loop bound)."""
        if self._drainer is None or self._queue is None:
            return
        await self._queue.join()
        self._drainer.cancel()
        try:
            await self._drainer
        except asyncio.CancelledError:
            pass
