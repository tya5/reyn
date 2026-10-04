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

#6240 ⑵ (architect ruling, issue #6240 comments 5807595460 + 5808598931; PR #6262 review found a
hole in the FIRST cut of this ruling — see below): the SAME per-burst boundary as
``on_drain_start`` above, at the OTHER end of ``_drain()``. ``Session`` holds a DEDICATED worker
for ``history.jsonl`` (constructed separately from the WAL/snapshot/audit/media workers — same
class, four separate instances, ``git grep 'DurabilityWorker(' -- src/`` shows the four call
sites), so this hook costs the OTHER three substrates nothing: ``on_drain_end`` (below) is
OPTIONAL, defaulted to ``None``, same discipline as ``on_drain_start``.

**Not** a post-loop callback invoked directly from the ``QueueEmpty`` branch (the ORIGINAL
shape this ruling shipped with) — PR review found that shape unobservable by ``flush``/
``aclose``, both of which wait on ``queue.join()``: every REAL item's ``task_done()`` already
ran by the time the ``QueueEmpty`` branch is reached, so ``join()`` can (and did) release before
a post-loop hook's own ``await`` even started. The fix (see :meth:`_drain`'s own docstring for
the mechanism): ``on_drain_end`` is ENQUEUED as an ordinary queue item on the first empty check
of a burst, and the loop only actually returns on the burst's SECOND empty check — so the hook
runs through :meth:`_run_with_retry` like any other write (a persistent failure gets the same
retry/escalation, not a bare unhandled exception) and its own ``task_done()`` is one ``join()``
WILL wait on — closing the gap by construction rather than by touching the barrier itself.
History's own use: fsync the durable append handle once per burst, not once per line — the
fsync count is bounded by drain bursts, not by append count.

#6261 (architect ruling, issue #6261): ``_ensure_queue`` rebinding to a new loop (a SECOND,
separate ``asyncio.run()`` call against the same worker — a test-reachable shape; production
runs one loop per process) used to just replace ``self._queue``, silently discarding whatever
the OLD loop's queue still held. Ruling: log-and-drop leaves the loss standing (an announced
loss is a record of loss, not a fix for it) and rejecting the rebind breaks legitimate reuse
while moving the failure OUTSIDE durability's own code, where nobody fixing it would think to
look. Instead the stale queue's contents are CARRIED FORWARD onto the fresh queue — see
:meth:`_ensure_queue`/:meth:`_carry_over_stale_queue` for the mechanics and the one case
(a BLOCKING ``submit`` awaiting a future the old, dead loop can never resolve) that is still
dropped, deliberately, because nobody is left to observe it. This is the SAME class of defect
``events.py``'s #4966 already closed for ``EventLog._ensure_consumer_started`` (a stale
reference to a dead loop's task, detected via ``task.done()`` and re-bound rather than
discarded) — #6261 answers it the same direction, one class over, so the repo does not hold
two opposite answers to the same shape of bug.
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
        a re-init, or this SAME worker driven through a SECOND, separate ``asyncio.run()`` call)
        gets a fresh queue + resets the drainer to None (the old loop's task is abandoned — inert;
        in production there is one loop). Does NOT start the drainer — callers enqueue FIRST, then
        ``_kick``, so the (self-terminating) drainer never sees an empty queue before the item
        lands.

        #6261 (architect ruling): a rebind used to just replace ``self._queue`` outright —
        whatever the OLD loop's queue still held was silently garbage-collected with it. Now
        the stale queue's own contents are carried forward onto the fresh queue FIRST (see
        :meth:`_carry_over_stale_queue`), before this call returns — so a caller enqueuing right
        after a rebind lands its own item AFTER whatever was carried over, preserving FIFO."""
        loop = asyncio.get_running_loop()
        if self._loop is not loop:
            stale_queue = self._queue
            self._queue = asyncio.Queue()
            self._loop = loop
            self._drainer = None  # old drainer (old loop) abandoned; _kick starts a fresh one
            if stale_queue is not None:
                self._carry_over_stale_queue(stale_queue)
        assert self._queue is not None
        return self._queue

    def _carry_over_stale_queue(self, stale_queue: "asyncio.Queue") -> None:
        """#6261 (architect ruling, issue #6261 — "移す, not log-and-drop, not raise"): called
        ONLY from :meth:`_ensure_queue`, exactly once per rebind, with the queue that USED to be
        bound to the loop that just stopped being the running one. This is the SAME class of
        defect ``events.py``'s #4966 already answered for ``EventLog._ensure_consumer_started``
        — a stale reference surviving a dead loop (there: a consumer ``Task``; here: a queue) —
        and #4966 chose "detect the staleness, carry the mechanism forward", never "discard
        silently". Answering #6261 with a plain drop would contradict that precedent one class
        over, in the SAME file family.

        Each queued item is ``(callable, future | None)`` (see :meth:`submit`/:meth:`submit_nowait`).
        Three shapes, three outcomes:

        * ``(callable, None)`` — a fire-and-forget write (``submit_nowait``/``submit_threadsafe``).
          Its callable touches no loop object itself (it only runs ``await
          asyncio.to_thread(...)`` internally), so it runs identically on ANY loop — MOVED onto
          the new queue via ``put_nowait`` (that queue's own accounting, never the stale one's).
          Moved in original (FIFO) order, and moved before ``_ensure_queue`` returns the new
          queue to its caller, so these items are always ahead of whatever the caller that
          triggered THIS rebind is about to enqueue.

        * ``(callable, future)`` — a BLOCKING ``submit`` still awaiting that exact future. The
          future was created on the OLD loop (``self._loop.create_future()`` at submit time);
          its only awaiter is a coroutine running ON that old loop. By construction, a rebind
          only happens when ``asyncio.get_running_loop()`` returns something else than
          ``self._loop`` — one process has one loop in production, so this is reachable only via
          a SECOND, separate ``asyncio.run()`` call (or an explicit second loop in a test) — and
          either way, that old loop is no longer running by the time this executes, so its
          awaiting coroutine can never be scheduled again. There is nobody left to hand this
          future to, so it is DROPPED — never silently: the count is logged below.

        * ``self._on_drain_end`` itself, enqueued with ``fut is None`` (see :meth:`_drain`'s own
          docstring, #6240 ⑵) — DROPPED, and counted separately from an ordinary fire-and-forget
          drop: it is a burst-boundary SIGNAL for the OLD (now-abandoned) burst, not durable
          data, so dropping it loses nothing a caller could observe. The NEW loop gets its OWN
          end-of-burst job at the true end of its OWN next ``_drain()`` call (:meth:`_drain`'s
          normal path), and that job's fsync already covers every write carried over here,
          PROVIDED they land on the same file — the module docstring's own "SAME FILE ONLY"
          retroactive-coverage limit applies unchanged. Carrying the stale job forward instead
          would only fsync twice, never add safety, and would need ``_drain``'s per-call
          ``drain_end_enqueued`` guard to recognise it as already-answered, which a plain queued
          item cannot signal.

        ``stale_queue``'s own ``_unfinished_tasks``/``join()`` bookkeeping is left untouched (no
        ``task_done()`` calls made against it): the only way to be blocked on
        ``stale_queue.join()`` is a coroutine running on the OLD loop (:meth:`flush`/
        :meth:`aclose` both bail out before ever reaching ``_drain_to_empty`` when the running
        loop is not the bound one — see their own docstrings), and that loop is the one that
        just stopped being the running loop — so no such waiter can exist by the time this
        runs. The stale queue object is simply left to be garbage-collected whole."""
        moved = 0
        dropped_awaited = 0
        dropped_stale_terminal_job = False
        while True:
            try:
                do_durable_write, fut = stale_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if self._on_drain_end is not None and do_durable_write is self._on_drain_end:
                dropped_stale_terminal_job = True
                continue
            if fut is None:
                assert self._queue is not None
                self._queue.put_nowait((do_durable_write, None))
                moved += 1
            else:
                dropped_awaited += 1
        if moved or dropped_awaited or dropped_stale_terminal_job:
            detail = [f"carried over {moved} fire-and-forget write(s)"]
            if dropped_awaited:
                detail.append(
                    f"dropped {dropped_awaited} awaited write(s) whose loop is gone "
                    "(nobody is left waiting)"
                )
            if dropped_stale_terminal_job:
                detail.append(
                    "dropped the stale end-of-burst job (superseded by the new loop's own)"
                )
            import logging  # noqa: PLC0415
            logging.getLogger(__name__).warning(
                "DurabilityWorker rebind (queue moved to a new event loop): %s.",
                "; ".join(detail),
            )

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

        No-stranding: the ``QueueEmpty`` check + ``return`` are atomic (no ``await`` between), so a
        concurrent ``submit`` cannot interleave there — an item enqueued while a prior one is
        processing is seen on the next iteration; an item enqueued after the drainer exits is
        picked up when the next submit re-kicks it (``_ensure_runtime`` restarts a ``done()``
        drainer).

        ``CancelledError`` MUST propagate (terminate the drainer): it is NOT a write failure.
        Swallowing it (catching ``BaseException``) made an earlier drainer immortal — a cancel
        landing mid-write was caught + the loop continued, and ``_cancel_all_tasks`` teardown hung
        forever. So a cancel resolves the in-flight future + re-raises; only a real ``Exception``
        (a write failure) is surfaced (to the submitter, or as the health-signal).

        #6077 提案 6 follow-up: ``on_drain_start`` (if set) runs ONCE here, before the loop below
        processes any queued item — this IS the "once per burst" checkpoint (see module
        docstring): every ``_drain()`` call is exactly one such burst, since the drainer is
        self-terminating and only re-``_kick``ed when a NEW item lands after it already exited.
        Left untouched by #6240 ⑵ below (architect ruling): a START hook runs BEFORE any item is
        processed, so it never needs ``queue.join()``'s own accounting to be correct — moving it
        would touch a boundary that has no defect.

        #6240 ⑵ (architect ruling, PR #6262 review — the FIRST cut of this ruling put
        ``on_drain_end`` in a post-loop hook here, called directly from the ``QueueEmpty`` branch;
        review found that placement unobservable by :meth:`flush`/:meth:`aclose`, both of which
        wait on ``queue.join()`` — ``task_done()`` is called for every REAL item before this
        branch is ever reached, so ``join()`` could release BEFORE a post-loop hook's own ``await``
        even started, let alone finished): ``on_drain_end`` (if set) is never called directly here
        at all. Instead it is ENQUEUED as an ordinary ``(task, None)`` item, so it runs through
        :meth:`_run_with_retry` like any other queued write (a persistent failure — e.g. ``fsync``
        racing a closed handle — gets the SAME retry/escalation treatment a real write gets, not a
        bare unhandled exception with nowhere to go) and gets its OWN ``task_done()`` call exactly
        like any other item — which is what makes ``queue.join()`` (the barrier ``flush()``/
        ``aclose()`` already rely on, #6260's own cancel-race argument at :meth:`_drain_to_empty`,
        20 lines this ruling does not re-open) account for it, with no change to the barrier
        itself. ``drain_end_enqueued`` below is a LOCAL — scoped to THIS ``_drain()`` call, i.e. to
        one burst, never a ``self.`` attribute — guarding against enqueuing it twice (the job's own
        completion would otherwise see ANOTHER empty queue and enqueue itself again, forever); a
        fresh ``_drain()`` call gets a fresh (``False``) guard, so the NEXT burst still gets its own
        end-of-burst job.

        WHERE the enqueue happens is load-bearing, not a style choice — a SECOND race, caught only
        by actually running the mechanism-level witness (``tests/runtime/
        test_6240_2_history_fsync_on_drain_end.py``'s witness ⑤), not by reasoning about it: a
        first attempt enqueued the job ONLY from the ``QueueEmpty`` branch below (i.e. AFTER
        dropping through with nothing left) — but ``asyncio.Queue.join()`` releases its waiters the
        INSTANT ``unfinished_tasks`` (put count minus ``task_done()`` count) touches zero, even
        momentarily, and a LATER ``put_nowait`` (which internally re-``clear()``s the queue's
        "finished" event) cannot un-release a waiter whose future was already resolved by that
        earlier zero-crossing — ``Event.set()`` is a one-way edge per waiter. Calling ``task_done()``
        for the LAST real item (whatever item's own dequeue leaves the queue empty) drops the count
        to zero BEFORE the end-of-burst job is enqueued on the NEXT iteration, so any ``flush()``/
        ``aclose()`` already waiting got released one iteration early — exactly the same class of
        gap this whole ruling exists to close, re-derived one layer down. The fix: the check for
        "does this burst still owe an end-of-burst job" happens right after ``get_nowait()``
        succeeds (there is no ``await`` between them, so nothing else can run in between) — if the
        queue is ALREADY empty at that point (this dequeue just emptied it) and the job has not been
        enqueued yet, it is put now, BEFORE this item's own processing/``task_done()`` below, so
        ``unfinished_tasks`` never has a chance to touch zero without the job already counted. The
        ``QueueEmpty`` branch's own enqueue is the fallback for the (degenerate, but real —
        :meth:`_drain_to_empty`'s inline branch can call this directly) case of a burst with ZERO
        real items ever dequeued.

        A residual, accepted gap: an item enqueued strictly DURING the end-of-burst job's own
        ``await`` (e.g. a real write's ``asyncio.to_thread`` dispatch yielding the loop) is still
        processed this same call (FIFO, same as any concurrent-append case the no-stranding
        paragraph above already covers) but will not get a SECOND end-of-burst job this call (the
        guard is already ``True``) — it is left for whatever NEXT burst a future ``submit``
        re-kicks. If none ever comes, that write's OWN durability is unaffected (it still landed
        on disk via ``f.flush()``) but this hook's OWN promise for it is deferred, not lost: an
        ``os.fsync`` on the same fd/file syncs every prior unsynced write too, so any later burst's
        end-of-burst job still covers it retroactively — architect ruling, PR #6262 review, with
        TWO limits on that "retroactively": (1) SAME FILE ONLY — if the deferred write crosses a
        ``Session``-owned seal (``_maybe_seal_active_history_segment``) before any later burst's
        own end-of-burst job runs, that job fsyncs the NEW active segment's fd, a DIFFERENT file,
        never reaching back into the now-sealed one; the seal itself closes this by fsync-ing the
        segment it is about to seal, immediately before closing that handle for good (session.py —
        the seal already knows it will never write to that file again, so it is the one remaining
        place that can). (2) TEARDOWN — if no later burst EVER comes (the session ends instead),
        there is no "next burst" to retroactively cover anything; :meth:`aclose`'s own unconditional
        end-of-life ``on_drain_end`` call closes THIS gap (see its own docstring) rather than this
        method being asked to somehow "wait for whatever comes next", which would (architect,
        quoted in PR #6262's review) turn a burst boundary that is DECIDED the moment the
        end-of-burst job is enqueued into one that is instead CHASED — the same overreach
        :meth:`flush`'s own promise deliberately does not make."""
        assert self._queue is not None
        if self._on_drain_start is not None:
            await self._on_drain_start()
        drain_end_enqueued = False
        while True:
            try:
                do_durable_write, fut = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                if self._on_drain_end is not None and not drain_end_enqueued:
                    drain_end_enqueued = True
                    self._queue.put_nowait((self._on_drain_end, None))
                    continue  # the job just enqueued is picked up on the NEXT iteration
                return  # drained -- self-terminate
            if (
                self._on_drain_end is not None
                and not drain_end_enqueued
                and self._queue.empty()
            ):
                # This dequeue just emptied the queue -- the item now in hand may be the
                # LAST real item of the burst. Enqueue the end-of-burst job HERE, before
                # this item's own task_done() below, never after: asyncio.Queue.join()
                # releases its waiters the INSTANT unfinished_tasks (put count - task_done
                # count) touches zero, and a later put_nowait's queue.clear() cannot
                # un-release a waiter already woken (Event.set() is a one-way edge; a
                # waiter's future is already resolved by the time clear() runs) -- #6262
                # review caught this exact transient-zero race in an earlier cut that
                # enqueued the job only from the QueueEmpty branch itself, one iteration
                # AFTER this item's task_done() had already dropped the count to zero and
                # released `flush()`/`aclose()` early. Keeping the count >= 1 continuously
                # (this item + the job now both counted) from the LAST real item through
                # to the end-of-burst job's own task_done() closes that window.
                drain_end_enqueued = True
                self._queue.put_nowait((self._on_drain_end, None))
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

        #6261 (architect ruling, PR review): the two branches below both return SILENTLY —
        never raising, never letting the caller tell "drained everything" apart from "did
        nothing" — but they are not the same shape:

        1. ``self._queue is None`` (or ``self._loop is None``) — truly unused; there is nothing
           anywhere to lose.
        2. ``running is not self._loop`` — this worker's queue is bound to a DIFFERENT loop than
           the one calling ``flush()`` right now. This is the SAME "different loop" #6261's own
           rebind fixes (see the module docstring / :meth:`_ensure_queue`) — but ``flush()``
           itself never calls ``_ensure_queue``, so calling it from loop B does NOT trigger
           #6261's carry-over: whatever the OLD loop (A) still had queued stays bound to A,
           untouched, until something actually enqueues (``submit``/``submit_nowait``/
           ``bind_to_running_loop``) FROM B and rebinds it THAT way. Before #6261, a queue left
           behind like this was headed for silent loss the moment such a rebind happened; now it
           is moved forward instead — but that is a property of the NEXT rebind, not of this
           call. This early return still means only "not from this loop, not right now" — never
           "already flushed" — and a caller that needs loop A's items actually driven onto disk
           must call ``flush()`` FROM loop A before it ends, or trigger a rebind (any submit)
           from loop B first and ``flush()`` again afterward. This method cannot tell those two
           situations apart and does not pretend to.

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
        one the queue is bound to — see :meth:`flush`'s own docstring (#6261) for why this early
        return is "not from this loop", never evidence that a rebind lost anything: the carry-over
        that answers #6261 happens on the NEXT rebind (``_ensure_queue``), not on this call.

        #6240 ⑵ (architect ruling, PR #6262 review): teardown gets an UNCONDITIONAL, one-time
        ``on_drain_end`` call even when NOTHING has changed since the last successful
        :meth:`flush` — closing the one gap that method's own docstring's "retroactive coverage"
        paragraph deliberately leaves open (a write landing strictly AFTER a burst's own
        end-of-burst job was enqueued is left for the NEXT burst; if no next burst ever comes
        because the session ends instead, nothing is left to retroactively cover it — architect:
        "burst の終わりは *決める* もので *追いかける* ものでは在りません", the SAME reasoning
        that keeps :meth:`flush` from being strengthened to chase every last write; teardown is
        exempt only because it, uniquely, knows there is no "next chance").

        This costs NO new line here: the ``await self._drain_to_empty()`` call directly below
        (unchanged) already provides it, given how :meth:`_drain` behaves per #6240 ⑵'s queue-item
        design. Once the background drainer has self-terminated (the steady state after any prior
        :meth:`flush`/:meth:`aclose`), :meth:`_drain_to_empty` takes its OWN inline-drain branch —
        a FRESH :meth:`_drain` call, unconditionally, regardless of whether the queue holds
        anything — and that fresh call's very first ``QueueEmpty`` (immediate, if nothing is
        queued) still enqueues-and-runs ONE end-of-burst job (the same fallback :meth:`_drain`'s
        own docstring names for "a burst with ZERO real items ever dequeued"). If the drainer is
        instead still ALIVE (``aclose()`` landing mid-burst), :meth:`_drain_to_empty`'s OTHER
        branch just ``await``s ``queue.join()`` — and that already-running burst's OWN
        end-of-burst job (enqueued the normal way, since real items were present) is what
        ``join()`` waits for, so coverage holds there too, by the SAME argument :meth:`flush`'s
        own witness already proves. Either way, by the time this call returns, one MORE
        end-of-burst job has just run that did not exist before it. A no-op for the other 3
        ``DurabilityWorker`` consumers (WAL/snapshot/audit, media): ``on_drain_end`` is ``None``
        there, so the extra ``_drain()`` cycle this triggers still runs, but its own no-op branch
        (``if self._on_drain_end is not None`` — see :meth:`_drain`) means it costs them nothing."""
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
