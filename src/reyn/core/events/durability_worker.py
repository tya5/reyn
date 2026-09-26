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

#6077 提案 6 follow-up (architect ruling): ``EventStore`` holds a session-lifetime file handle
across writes (collapses ``open``/``close`` from once-per-event to ~once-per-rotation) — which
needs a way to detect the file being replaced/deleted out from under that held-open handle
(POSIX: a ``write`` through a stale handle succeeds SILENTLY, landing in an orphaned inode no
reader of the path will ever see again). Checking this on every write reintroduces a per-write
cost; architect's ruling uses a boundary THIS class already has instead of adding one: ``_drain``
is SELF-TERMINATING (drains until the queue is EMPTY, then exits — see its own docstring), so one
``_drain()`` call is already "one burst" of queued work. ``on_drain_start`` (below) is an
OPTIONAL hook — defaulted to ``None`` deliberately, since WAL/snapshot substrates use this SAME
class and must pay nothing for a concern that is EventStore's alone — invoked once at the top of
each ``_drain()`` call, before it processes any queued item. Cost now scales inversely with load:
one check per N queued writes during a burst (the situation where a per-write cost would have
mattered), one check per write only when idle (where the cost never mattered). The worker itself
carries no opinion about WHAT the hook checks — same substrate-agnostic discipline as the
``DurableWrite`` callable itself.

#6240 ⑵ (architect ruling, issue #6240 comments 5807595460 + 5808598931): the SAME per-burst
boundary, mirrored at the OTHER end of ``_drain()``. ``Session`` holds a DEDICATED worker for
``history.jsonl`` (constructed separately from the WAL/snapshot/audit/media workers — same
class, four separate instances, ``git grep 'DurabilityWorker(' -- src/`` shows the four call
sites), so this hook costs the OTHER three substrates nothing: ``on_drain_end`` (below) is
OPTIONAL, defaulted to ``None``, same discipline as ``on_drain_start``. Invoked once, right
before ``_drain()`` self-terminates on an EMPTY queue (never on the ``CancelledError`` exit —
a cancel mid-write is a #6261-owned hole, not this hook's concern; fsync-ing during teardown
would race it). Because BOTH of ``_drain``'s callers — the self-terminating background task
AND :meth:`_drain_to_empty`'s own inline-drain branch (``flush``/``aclose`` when no live
drainer is left to hand the queue to) — bottom out in this SAME ``_drain()`` body, wiring the
hook here (never on a background ``Task``'s ``add_done_callback``, which the inline branch
never creates) is the ONE place that covers both. History's own use: fsync the durable append
handle once per burst, not once per line — the fsync count is bounded by drain bursts, not by
append count.
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
        on_drain_start: "Callable[[], Awaitable[None]] | None" = None,
        on_drain_end: "Callable[[], Awaitable[None]] | None" = None,
    ) -> None:
        self._max_write_attempts = max_write_attempts
        self._retry_base_s = retry_base_s
        self._retry_max_s = retry_max_s
        # #6077 提案 6 follow-up: OPTIONAL, defaulted None — see module
        # docstring. Only EventStore passes one today; WAL/snapshot workers
        # (the same class, constructed elsewhere) never touch this and pay
        # nothing.
        self._on_drain_start = on_drain_start
        # #6240 ⑵: OPTIONAL, defaulted None — see module docstring. Only
        # Session's dedicated history worker passes one today; the other
        # three DurabilityWorker instances (WAL/snapshot/audit, media) never
        # touch this and pay nothing.
        self._on_drain_end = on_drain_end
        self._queue: "asyncio.Queue | None" = None
        self._drainer: "asyncio.Task | None" = None
        self._loop: "asyncio.AbstractEventLoop | None" = None
        # #2259 PR-2b: a fire-and-forget (non-blocking) durable write that fails PERSISTENTLY
        # (§4 retry-exhausted) has no submitter to re-raise to, so its escalation is a
        # health-signal: this latches True + a CRITICAL log. The system is no longer durably
        # persisting — a supervisor reads `durability_failed` to fail-stop. Never auto-cleared.
        self._durability_failed = False
        # #6260: True while THIS coroutine (inside `_drain_to_empty`) is acting as the
        # drainer itself — see that method's own docstring for why a second consumer
        # must never run concurrently. Checked + set with no `await` between (atomic
        # under cooperative scheduling), so a concurrent `flush`/`aclose`/`_kick` caller
        # sees the claim before it could ever race it.
        self._inline_draining = False

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
        item is enqueued, so the drainer is guaranteed to see it. #6260: a no-op while
        `_inline_draining` is True — `flush`/`aclose` is ALREADY the sole consumer at that
        point (see `_drain_to_empty`'s docstring); spawning a background task here too would
        give the queue two concurrent consumers and break FIFO = durability order. The item
        just enqueued is not stranded: `_drain`'s own no-stranding note applies unchanged —
        either the inline drain (still looping) picks it up on its next iteration, or (if it
        already exited) the NEXT `submit`/`submit_nowait` call re-kicks a fresh drainer."""
        if self._inline_draining:
            return
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

    def bind_to_running_loop(self) -> None:
        """#5898: bind this worker's queue to the CURRENTLY running loop
        without enqueuing anything — so a later :meth:`submit_threadsafe`
        from a worker thread has a loop to hand the job to. Called from
        ``MediaStore.flush`` (the barrier every chat turn takes on the loop
        before its first LLM call, i.e. before any off-loop tool-result
        processing could try to submit). Idempotent; same rebind rule as
        :meth:`_ensure_queue` (a fresh loop gets a fresh queue)."""
        self._ensure_queue()

    def submit_threadsafe(self, do_durable_write: DurableWrite) -> bool:
        """#5898: :meth:`submit_nowait` for a caller OFF the loop thread (a
        ``to_thread`` worker) — hands the enqueue to the bound loop via
        ``call_soon_threadsafe`` so the queue's own single-loop contract
        (``put_nowait`` on the loop thread, FIFO = durability order) is
        untouched. Returns ``False`` when no loop is bound (or it stopped)
        — the caller then falls back to its own no-loop path; never raises
        for that case. Ordering: jobs from one worker thread reach the
        queue in the order this is called (``call_soon_threadsafe`` is
        FIFO per loop), so a content write and its manifest line (the pair
        ``MediaStore.save_tool_result`` submits back-to-back) keep their
        order exactly as on-loop."""
        loop = self._loop
        if loop is None or loop.is_closed() or not loop.is_running():
            return False
        loop.call_soon_threadsafe(self.submit_nowait, do_durable_write)
        return True

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

        No-stranding: with ``on_drain_end`` unset, the ``QueueEmpty`` check + ``return`` are atomic
        (no ``await`` between), so a concurrent ``submit`` cannot interleave there — an item
        enqueued while a prior one is processing is seen on the next iteration; an item enqueued
        after the drainer exits is picked up when the next submit re-kicks it (``_ensure_runtime``
        restarts a ``done()`` drainer). With ``on_drain_end`` SET, its ``await`` DOES sit between
        the check and the return — so a ``submit`` landing during that ``await`` would otherwise be
        stranded (``_kick`` sees this drainer as not-``done()`` yet and declines to spawn a second
        one, trusting THIS loop to pick the item up — a trust this exit was about to betray). The
        re-check below (``if not self._queue.empty(): continue``) closes that window: the loop
        re-enters instead of returning whenever the hook's own ``await`` let something else land.

        ``CancelledError`` MUST propagate (terminate the drainer): it is NOT a write failure.
        Swallowing it (catching ``BaseException``) made an earlier drainer immortal — a cancel
        landing mid-write was caught + the loop continued, and ``_cancel_all_tasks`` teardown hung
        forever. So a cancel resolves the in-flight future + re-raises; only a real ``Exception``
        (a write failure) is surfaced (to the submitter, or as the health-signal).

        #6077 提案 6 follow-up: ``on_drain_start`` (if set) runs ONCE here, before the loop below
        processes any queued item — this IS the "once per burst" checkpoint (see module
        docstring): every ``_drain()`` call is exactly one such burst, since the drainer is
        self-terminating and only re-``_kick``ed when a NEW item lands after it already exited.

        #6240 ⑵: ``on_drain_end`` (if set) runs ONCE here too, right before the self-terminating
        ``return`` on an EMPTY queue — the OTHER end of the same burst boundary. Both of this
        method's callers (the background self-terminating drainer task, AND
        :meth:`_drain_to_empty`'s own inline-drain branch, which calls ``await self._drain()``
        directly with no intervening ``Task``) bottom out in THIS body, so wiring the hook here
        — never externally, e.g. via a ``Task.add_done_callback`` the inline branch would never
        create — is the one place that reaches both. Never runs on the ``CancelledError`` exit
        below (a cancel is not "drained"; fsync-ing mid-teardown is #6261's own territory, not
        this hook's — see :class:`DurabilityWorker`'s own module docstring)."""
        assert self._queue is not None
        if self._on_drain_start is not None:
            await self._on_drain_start()
        while True:
            try:
                do_durable_write, fut = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                if self._on_drain_end is not None:
                    await self._on_drain_end()
                    if not self._queue.empty():
                        continue  # something landed during the hook's own await -- re-drain it
                return  # drained → self-terminate
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

    async def _drain_to_empty(self) -> None:
        """#6260: the ONE shared "wait until the queue is fully drained" body used by both
        :meth:`flush` and :meth:`aclose` — draining is never written twice.

        There must be exactly ONE consumer pulling off ``_queue`` at a time (two consumers
        would race each other's ``get_nowait``, breaking FIFO = durability order): if the
        background drainer is alive AND not (already, or about to be) cancelled, it already IS
        that consumer — kick it (only if idle: a queued item the ``_kick`` at the enqueue site
        already scheduled it to drain) and wait on ``queue.join()``, exactly as before #6260.

        "Alive" is checked two ways, not one, because a SINGLE ``done()`` check is stale the
        instant it matters: ``not self._drainer.done()`` alone would still read True for a task
        that was JUST ``.cancel()``-ed but has not yet been scheduled to actually process that
        cancellation (Python only delivers ``CancelledError`` at the task's NEXT step) — taking
        the "alive, just wait" branch there is exactly the #6260 hang, because that task, once
        it IS scheduled, may terminate having drained ZERO items (a task cancelled before its
        very first step never even reaches its own ``get_nowait`` loop) while ``join()`` sits
        waiting on ``task_done()`` calls nobody is left to make. ``Task.cancelling() > 0``
        (Python 3.11+, the SAME discriminator :meth:`aclose`'s own ``except CancelledError``
        clause already uses below) is set SYNCHRONOUSLY by ``.cancel()`` — no scheduling delay
        — so it catches this the instant it happens, not just after ``done()`` eventually
        catches up.

        If the drainer is missing / finished / cancelled / cancelling, there is no other
        consumer left to reach the ``task_done()`` calls ``join()`` would otherwise wait on —
        the #6260 hang: a drainer killed mid-queue (production reachable via ``AgentRegistry.
        shutdown``'s hard-cancel, or loop teardown on ``/quit``/Ctrl-C) leaves the remaining
        items permanently "unfinished". So THIS coroutine claims the drainer role itself and
        drives :meth:`_drain` directly — no NEW task is created, so completing it never depends
        on the loop being willing to schedule one more task while it may already be tearing
        down. ``_inline_draining`` is the claim: set for the duration (with ``_kick`` refusing
        to spawn a second consumer while it is set — see that method), so a second concurrent
        caller sees it and falls back to ``queue.join()`` instead of draining a second time.
        This is fail-CLOSED, not fail-open: every currently-enqueued write still drains (the
        docstring on :meth:`flush` keeps its word) — cancellation just stops depending on a
        task nobody may run again."""
        assert self._queue is not None
        drainer = self._drainer
        if drainer is not None and not drainer.done() and drainer.cancelling() == 0:
            if not self._queue.empty():
                self._kick()
            await self._queue.join()
            return
        if self._inline_draining:
            await self._queue.join()
            return
        self._inline_draining = True
        try:
            await self._drain()
        finally:
            self._inline_draining = False

    async def flush(self) -> None:
        """#2259 PR-2b: wait until every currently-enqueued durable write has DRAINED — WITHOUT
        closing the worker (it stays usable). For any caller that must observe a fire-and-forget
        write's effect (e.g. a test asserting a truncate's result, or a deliberate barrier).
        Same loop-guard as ``aclose``; a no-op if never used or called on a different loop.

        #6260: never hangs on a drainer that died mid-queue (cancelled by something else,
        e.g. ``AgentRegistry.shutdown``'s hard-cancel or loop teardown) — see
        :meth:`_drain_to_empty`."""
        if self._queue is None or self._loop is None:
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is not self._loop:
            return
        await self._drain_to_empty()

    async def aclose(self) -> None:
        """Graceful shutdown. Drain every enqueued task (no in-flight write lost), then stop —
        via the SAME :meth:`_drain_to_empty` :meth:`flush` uses (#6260) — then cancel any
        still-running drainer. A no-op if never used, or if called on a different loop than the
        one the queue is bound to (a dead loop — nothing to drain there)."""
        if self._queue is None or self._loop is None:
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is not self._loop:
            return
        await self._drain_to_empty()
        if self._drainer is not None and not self._drainer.done():
            self._drainer.cancel()
            try:
                await self._drainer
            except asyncio.CancelledError:
                # #4988: `await self._drainer` raises CancelledError either
                # as the drainer task's own outcome (this method's own
                # `.cancel()` two lines up — what this except exists to
                # absorb) or as an independent, external cancellation of
                # THIS coroutine's own task landing at the same await.
                # `pass`-ing unconditionally used to treat both the same,
                # letting `aclose()` return normally even when its own
                # caller was being cancelled. Same discriminator as
                # session.py's #3377 precedent (`_driver.cancelling() > 0`).
                _current = asyncio.current_task()
                if _current is not None and _current.cancelling() > 0:
                    raise
