"""``ThreadedTransportProxy`` — the #4995 thread boundary for ``ClientTransport``.

**Redesign history, kept honest** (this issue went through 2 rejected
approaches before settling — both measured, not guessed):

1. First attempt: offload individual CPU-heavy functions (elide accounting,
   turn serialise, event-log emit) one at a time via ``asyncio.to_thread``,
   each at its own call site. REJECTED (architect, self-flagged as their own
   issue-body residue): "N seams (one per function)" where "1 seam (the
   transport)" already exists and already has the properties a thread
   boundary needs — a single writer, an ordered stream. ``in_process.py``'s
   own docstring already states this: "the client writes to the world ONLY
   through this seam (single-writer)".
2. Second attempt (the fix for ①): ``asyncio.to_thread(build_history)`` at
   its 2 call sites, keeping ``ChatReadModel`` reads synchronized with a
   ``threading.Lock``. REJECTED (lead-coder, measured self-contradiction):
   a lock held by the core thread while it does I/O (``_load_older_
   entries``) is exactly the "TUI does not get scheduled" symptom #4995
   exists to remove, just moved from the event loop to a mutex — the very
   witness this issue requires (UI advances while the worker is held open)
   would go red under this design.
3. Settled: **immutable snapshot hand-off, not shared mutable state**. The
   core thread OWNS ``Session``/``AgentRegistry`` exclusively — nothing on
   the caller (TUI) thread ever reads their live, mutable attributes
   directly. Every value the caller thread needs (frames, ``has_session()``,
   ``pending_intervention_head()``, etc.) is refreshed into ONE overwriting
   slot (:class:`_ThreadedSnapshot`) each time the core thread produces a
   frame — the same moment production code already treats as "something the
   client should learn about" (:mod:`reyn.interfaces.transport.frames`'s own
   unified-stream design). No lock is needed because there is no shared
   MUTABLE state to protect — the slot holds a fresh, frozen dataclass
   instance each time, and a single attribute assignment is a GIL-atomic
   pointer swap in CPython, never a partial/torn read.

This is genuinely an EXTENSION of the existing ``call_soon_threadsafe``
mechanism the frame stream already needs (a frame produced on the core
thread must reach the caller thread's ``asyncio.Queue`` via
``call_soon_threadsafe`` regardless of this class), not a second mechanism
running alongside it — the snapshot slot is written in the SAME callback
that schedules the frame delivery.

**Scope, explicit** (per lead-coder's #4995 ruling): 2 ``ChatReadModel``
methods were NOT satisfiable by this snapshot design at #4995's own
landing and were deliberately left unwired here — see #5044.
``load_older_conversation_history()`` mutated ``Session.history`` and
performed disk I/O (confirmed by reading it, not guessed) — landed via
``Session.extend_history_backward_async``'s own I/O-off-loop/apply-on-loop
split, #5079. ``completion_source()`` (renamed from ``completion_session()``
— the old name promised a ``Session``, the actual contract never fit one)
returned a LIVE ``Session`` reference, not a copyable value; fixed by
returning a ``CompletionSourceSnapshot`` VALUE instead, #5087 — still not
wired through THIS class's own snapshot-refresh cadence (a future step,
should this class ever gain a production call site).

#5045 (``clear_pending_command_ui()`` was a
WRITE living on a type named "read model") is CLOSED — the write moved
to :meth:`ClientTransport.clear_pending_command_ui`, and this class's own
:meth:`clear_pending_command_ui` override marshals it onto the worker
thread the same way every other write above does. Wiring THIS class in
as ``TextualChatApp``'s / ``run_repl``'s default local transport is left
to a follow-up (#5048) — this PR's deliverable is the mechanism itself,
with real witnesses, not the production cutover.

**As of 2026-08-22, this class has NO production call site** —
:class:`ThreadedTransportProxy` is constructed only from
``tests/interfaces/test_4995_threaded_transport_proxy.py``.
``TextualChatApp``/``run_repl`` still construct ``InProcessTransport``
directly; wiring this proxy in is #5048's own deliverable, not this
one's. Read this line here, not only in the PR body or the test
module's own docstring — a PR body is history nobody re-reads once
merged, and a test docstring is read only by whoever opens the test;
this is the one place a future reader of THIS file's own code will
actually see the gap (the same "declared but nobody reads it" shape
named 3 times tonight, e.g. #5033's own disclosure-line finding).
"""
from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, AsyncIterator, Callable, Coroutine

from reyn.interfaces.transport.client_transport import ClientTransport, pending_head_id
from reyn.interfaces.transport.drain import suspend_between_frames

if TYPE_CHECKING:
    from pathlib import Path

    from reyn.interfaces.transport.frames import DisplayFrame, EventFrame
    from reyn.runtime.outbox import OutboxMessage


@dataclass(frozen=True)
class _ThreadedSnapshot:
    """The frozen, single-slot value the caller (TUI) thread reads — every
    field here mirrors a SYNCHRONOUS ``ClientTransport`` accessor
    (:meth:`ClientTransport.has_session` and its 3 siblings) that would
    otherwise read the core thread's live, mutable state directly. Refreshed
    as ONE unit each time the core thread produces a frame (see this
    module's own docstring for why one slot, not 4 separately-timestamped
    caches, and why no lock is needed).

    ``read_model_snapshot`` piggybacks the SAME refresh — a caller wiring a
    thread-local ``ChatReadModel`` (e.g. ``RegistryReadModel``) over this
    proxy reads ITS OWN ``snapshot()`` from here rather than reaching into
    the registry directly from the caller thread, closing the same
    TUI-reads-vs-core-writes race this whole class exists to remove.
    ``None`` before the first frame arrives — the SAME pre-attach contract
    ``project_remote_snapshot``'s callers already have (#5009's own
    ``cost_pane_lines(None)``/``ctx_pane_lines(None)`` degrade), not a new
    third state invented for this class."""

    has_session: bool
    attach_failed: bool
    pending_intervention_head: "str | None"
    reyn_state_root: "Path | None"
    read_model_snapshot: "dict | None"


_EMPTY_SNAPSHOT = _ThreadedSnapshot(
    has_session=False,
    attach_failed=False,
    pending_intervention_head=None,
    reyn_state_root=None,
    read_model_snapshot=None,
)


class ThreadedTransportProxy(ClientTransport):
    """Runs a real ``ClientTransport`` (and whatever it wraps — a
    ``Session``/``AgentRegistry``, in production) on a DEDICATED worker
    thread with its own event loop, presenting the identical
    ``ClientTransport`` surface to a caller on a DIFFERENT thread (the TUI's
    own asyncio loop).

    ``transport_factory`` is called ON THE WORKER THREAD, inside its own
    loop — deliberately deferred construction, not a transport built on the
    caller thread and handed over, so the registry/session it wraps is
    OWNED by the worker thread from the moment it exists. Nothing about the
    inner transport is ever touched from the caller thread except through
    this proxy's own methods.

    ``read_model_snapshot_fn`` is optional — a zero-arg callable (typically
    a thread-local ``ChatReadModel.snapshot``, e.g. ``RegistryReadModel``
    bound to the SAME registry the worker thread owns) called ON THE WORKER
    THREAD alongside every frame, whose result rides in
    :class:`_ThreadedSnapshot` for the caller thread to read.

    ``read_model_extend_history_fn`` is optional too — an ASYNC
    ``(agent, session_id) -> int`` callable (typically the SAME
    ``RegistryReadModel``'s bound ``Session.extend_history_backward_
    async``) marshalled onto the worker thread ON DEMAND via
    :meth:`extend_history_backward`, unlike ``read_model_snapshot_fn``
    which runs unconditionally alongside every frame. #5044: this one
    genuinely MUTATES ``Session.history`` and performs disk I/O — not
    satisfiable by the snapshot design's read-only push, so it gets its
    own pull-style cross-thread call instead (mirroring
    :meth:`_call_on_worker`'s own ``run_coroutine_threadsafe`` +
    ``wrap_future`` shape, but for a caller-supplied closure rather than
    one of ``self._inner``'s own methods).

    #5079/#4995 (architect ruling, issuecomment-5378398588): the supplied
    closure is expected to be ASYNC and to do its OWN "read off the loop,
    apply on the loop" split internally — mirroring #4983's own precedent
    in ``textual_chat/app.py`` (``_handle_session_attached_event``: step
    ① the disk read runs OFF the event loop via ``asyncio.to_thread``,
    step ② the small in-memory apply stays on the loop). This proxy does
    NOT impose that split itself — it is transport-generic and has no
    Session-specific knowledge of what "the read" vs "the apply" even
    are; it only marshals whatever coroutine the closure returns onto the
    worker loop and awaits it, so a closure that internally awaits
    ``asyncio.to_thread(...)`` genuinely frees the worker loop for that
    span, while the closure's own final synchronous mutation step still
    runs safely on the loop that owns ``Session``.

    Delegation is total and explicit: every public ``ClientTransport``
    method is defined directly on THIS class's own body — never silently
    inherited from ``ClientTransport``'s own base default — enforced by
    ``test_threaded_transport_proxy_total_delegation_5048.py`` (#5048,
    mirroring #4884's identical claim/gate pairing for
    ``_ErrorWatchingTransport``). A method missing here would fall back to
    the base default across the WORKER-THREAD boundary specifically —
    silently answering with a value that reflects nothing about the real
    inner transport's state, not merely a generic wrong answer.
    """

    def __init__(
        self,
        transport_factory: "Callable[[], ClientTransport]",
        *,
        read_model_snapshot_fn: "Callable[[], dict] | None" = None,
        read_model_extend_history_fn: (
            "Callable[[str | None, str | None], Coroutine[Any, Any, int]] | None"
        ) = None,
    ) -> None:
        self._transport_factory = transport_factory
        self._read_model_snapshot_fn = read_model_snapshot_fn
        self._read_model_extend_history_fn = read_model_extend_history_fn
        self._inner: "ClientTransport | None" = None
        self._worker_loop: "asyncio.AbstractEventLoop | None" = None
        self._thread: "threading.Thread | None" = None
        self._worker_thread_ident: "int | None" = None
        self._caller_loop: "asyncio.AbstractEventLoop | None" = None
        self._caller_queue: "asyncio.Queue[DisplayFrame | EventFrame] | None" = None
        # Single overwriting slot (#4995's own settled design, see module
        # docstring) — a plain attribute, not a queue: only the LATEST
        # snapshot is ever meaningful, so an older one is safe to discard
        # outright rather than accumulate (CLAUDE.md six-questions #5 — this
        # is bounded by the TYPE, not by a discipline of "don't push too
        # often").
        self._latest: "_ThreadedSnapshot" = _EMPTY_SNAPSHOT
        self._ready = threading.Event()
        self._pump_task: "asyncio.Task | None" = None

    # -- lifecycle ------------------------------------------------------

    @property
    def worker_thread_ident(self) -> "int | None":
        """The worker thread's ``threading.get_ident()`` — witness① reads
        this directly (structural: is the work genuinely elsewhere)."""
        return self._worker_thread_ident

    def start(self) -> None:
        self._caller_loop = asyncio.get_event_loop()
        self._caller_queue = asyncio.Queue()
        self._thread = threading.Thread(
            target=self._run_worker, name="reyn-core-worker", daemon=True,
        )
        self._thread.start()
        # Bounded by CI's own kill switch (CLAUDE.md floor/ceiling rule) —
        # not a sleep, not an attempts=N poll: an unbounded wait on a
        # condition the worker thread itself sets the instant its loop and
        # inner transport exist.
        self._ready.wait()

    def _run_worker(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._worker_loop = loop
        self._worker_thread_ident = threading.get_ident()
        self._inner = self._transport_factory()
        self._inner.start()
        self._pump_task = loop.create_task(self._pump_frames())
        self._ready.set()
        loop.run_forever()

    async def _pump_frames(self) -> None:
        """Runs ON THE WORKER LOOP. Drains the inner transport's own frame
        stream and, for EACH frame, refreshes the single snapshot slot
        BEFORE handing the frame to the caller thread — a reader that sees
        the frame is guaranteed to see a snapshot at least that fresh."""
        assert self._inner is not None
        async for frame in self._inner.frames():
            self._latest = _ThreadedSnapshot(
                has_session=self._inner.has_session(),
                attach_failed=self._inner.attach_failed(),
                pending_intervention_head=pending_head_id(
                    self._inner.pending_intervention_head(),
                    caller="ThreadedTransportProxy",
                ),
                reyn_state_root=self._inner.reyn_state_root(),
                read_model_snapshot=(
                    self._read_model_snapshot_fn()
                    if self._read_model_snapshot_fn is not None
                    else None
                ),
            )
            assert self._caller_loop is not None and self._caller_queue is not None
            self._caller_loop.call_soon_threadsafe(
                self._caller_queue.put_nowait, frame,
            )

    def close(self) -> None:
        if self._worker_loop is None:
            return
        self._worker_loop.call_soon_threadsafe(self._close_on_worker)

    def _close_on_worker(self) -> None:
        if self._inner is not None:
            self._inner.close()

    async def frames(self) -> "AsyncIterator[DisplayFrame | EventFrame]":
        assert self._caller_queue is not None
        while True:
            frame = await self._caller_queue.get()
            # #3570: unconditional suspension point, once per frame — same
            # reasoning as ``InProcessTransport.frames``'s own identical
            # line (see ``drain.py``'s own docstring): ``Queue.get()``
            # returns without suspending whenever the queue is non-empty, so
            # a burst of frames pushed by the worker thread between two
            # visits of this generator would otherwise drain to exhaustion
            # with the CALLER's loop never running anything else.
            await suspend_between_frames()
            yield frame

    # -- caller-thread reads: the single snapshot slot -------------------

    def has_session(self) -> bool:
        return self._latest.has_session

    def attach_failed(self) -> bool:
        return self._latest.attach_failed

    def pending_intervention_head(self) -> "str | None":
        return self._latest.pending_intervention_head

    def reyn_state_root(self) -> "Path | None":
        return self._latest.reyn_state_root

    def read_model_snapshot(self) -> "dict | None":
        """Not part of ``ClientTransport`` — the seam a thread-local
        ``ChatReadModel`` (e.g. a ``RegistryReadModel`` subclass bound to
        this proxy) calls instead of reading the registry directly."""
        return self._latest.read_model_snapshot

    async def extend_history_backward(
        self, *, agent: "str | None" = None, session_id: "str | None" = None,
    ) -> int:
        """Not part of ``ClientTransport`` either — #5044's cross-thread
        sibling to ``read_model_snapshot`` above, for the ONE
        ``ChatReadModel`` operation the snapshot design cannot satisfy:
        ``RegistryReadModel.load_older_conversation_history`` genuinely
        MUTATES ``Session.history`` and performs disk I/O (confirmed by
        reading it, not guessed — #5044's own issue body), so a caller
        needs the REAL, POST-mutation count back, not a value that was
        already frozen into a snapshot slot before this call happened.

        Schedules ``read_model_extend_history_fn`` (an ASYNC closure — see
        this class's own docstring for why) ON THE WORKER THREAD's loop
        (via ``run_coroutine_threadsafe`` + ``wrap_future`` — the SAME
        non-blocking shape :meth:`_call_on_worker` gives every ASYNC
        ``ClientTransport`` method below) and awaits the real result. The
        closure's own internal ``await`` points (#5079/#4995: its own
        off-loop disk read) run exactly where scheduled — this call
        itself does not block the worker loop for their duration, only
        for the closure's brief synchronous portions. Returns ``0``
        (already-exhausted, never a fabricated count — same convention
        ``ChatReadModel.load_older_conversation_history``'s own docstring
        states for its remote impl) when no callable was supplied at
        construction, or when this proxy has not been started yet."""
        if self._read_model_extend_history_fn is None or self._worker_loop is None:
            return 0
        fn = self._read_model_extend_history_fn

        concurrent_future = asyncio.run_coroutine_threadsafe(
            fn(agent, session_id), self._worker_loop,
        )
        return await asyncio.wrap_future(concurrent_future)

    # -- caller-thread writes: marshalled onto the worker loop -----------

    def put_display(self, msg: "OutboxMessage") -> None:
        # Fire-and-forget onto the worker loop — ``inner.put_display``'s own
        # queue is an ``asyncio.Queue`` owned by that loop, not safe to
        # touch (``put_nowait``) from a foreign thread directly.
        if self._worker_loop is None or self._inner is None:
            return
        self._worker_loop.call_soon_threadsafe(self._inner.put_display, msg)

    async def _call_on_worker(self, name: str, *args: Any, **kwargs: Any) -> Any:
        """Generic dispatch for every ASYNC ``ClientTransport`` method this
        proxy forwards — one shared helper rather than N near-identical
        ``run_coroutine_threadsafe`` + ``wrap_future`` bodies (the shape
        #5027 already measured and collapsed for ``reported_snapshot_
        keys``: N call sites is N places a future method could be wired to
        only 1). ``asyncio.wrap_future`` is what makes this genuinely
        non-blocking for the CALLER's own loop — the caller ``await``s a
        real asyncio Future tied to its own loop, which only resolves once
        the worker loop's coroutine completes; the caller loop keeps
        scheduling everything else (this IS witness②'s mechanism, not a
        second one)."""
        assert self._worker_loop is not None and self._inner is not None
        method = getattr(self._inner, name)
        concurrent_future = asyncio.run_coroutine_threadsafe(
            method(*args, **kwargs), self._worker_loop,
        )
        return await asyncio.wrap_future(concurrent_future)

    async def submit_user_text(self, text: str) -> str:
        return await self._call_on_worker("submit_user_text", text)

    async def answer_intervention_text(
        self, text: str, *, intervention_id: "str | None" = None,
    ) -> bool:
        return await self._call_on_worker(
            "answer_intervention_text", text, intervention_id=intervention_id,
        )

    async def answer_intervention_choice(
        self, choice_id: str, *, intervention_id: "str | None" = None,
    ) -> bool:
        return await self._call_on_worker(
            "answer_intervention_choice", choice_id, intervention_id=intervention_id,
        )

    async def state_ready(self) -> None:
        # #5050 ③ follow-up (lead-coder, issuecomment-5377682724 — the
        # 3rd lying-ready site found across the 3 production wrappers
        # this axis had to be threaded through): the base default (return
        # immediately) would be WRONG here specifically because the inner
        # transport's ``state_ready()`` awaits an ``asyncio.Event`` that
        # lives on the WORKER loop, not the caller's — falling through to
        # the base default skips the only place that Event is ever
        # actually waited on. ``_call_on_worker`` (not a bespoke bridge)
        # marshals the coroutine onto the worker loop exactly like every
        # other delegated async method above.
        await self._call_on_worker("state_ready")

    async def clear_pending_command_ui(self) -> None:
        # #5045: the real write, marshalled onto the worker thread that
        # owns the Session — same reasoning as state_ready() just above:
        # ClientTransport's own base default (a no-op) would silently
        # fail to clear anything, since the inner transport's own
        # InProcessTransport override is what actually calls
        # Session.set_pending_command_ui, and that Session object is
        # owned by the WORKER thread, never safe to touch directly from
        # the caller thread.
        await self._call_on_worker("clear_pending_command_ui")

    async def cancel_inflight(self) -> str:
        return await self._call_on_worker("cancel_inflight")

    async def cancel_queued(self, msg_id: str) -> bool:
        return await self._call_on_worker("cancel_queued", msg_id)

    async def request_mcp_retry(self, server: str) -> bool:
        return await self._call_on_worker("request_mcp_retry", server)

    async def run_slash_command(self, name: str, args: str) -> bool:
        return await self._call_on_worker("run_slash_command", name, args)

    async def request_attach(self, agent_name: str) -> bool:
        return await self._call_on_worker("request_attach", agent_name)

    async def request_session_switch(self, session_id: str) -> bool:
        return await self._call_on_worker("request_session_switch", session_id)

    async def request_artifact_list(
        self, *, agent: str,
    ) -> "tuple[list[dict], int]":
        return await self._call_on_worker("request_artifact_list", agent=agent)

    async def request_session_list(self) -> "list[dict]":
        return await self._call_on_worker("request_session_list")

    async def request_older_backlog(self, before_root_id: str) -> None:
        await self._call_on_worker("request_older_backlog", before_root_id=before_root_id)

    async def _cancel_pump_on_worker(self) -> None:
        """Runs ON THE WORKER LOOP (via ``run_coroutine_threadsafe``) so
        cancelling the pump task is properly awaited before this proxy asks
        the loop to stop — a bare ``call_soon_threadsafe(pump_task.cancel)``
        with no await races the loop's own stop: cancellation is only
        delivered on the task's NEXT resumption, which may not happen
        before a fire-and-forget stop already ends ``run_forever()``,
        leaving the task "destroyed but pending" on teardown.

        Deliberately does NOT call ``loop.stop()`` itself — a task that
        stops its OWN loop as its last statement races its own "done"
        callback delivery (``Task.set_result`` schedules callbacks via
        ``call_soon`` for a LATER iteration; ``run_forever`` can already
        have exited by then), which is exactly what left ``shutdown()``
        hanging on ``wrap_future`` forever the first time this was written
        — the loop never ran the callback that would have copied this
        coroutine's result into the caller-visible future. ``shutdown()``
        stops the loop itself, as a separate step, once this has
        genuinely returned."""
        if self._pump_task is not None:
            self._pump_task.cancel()
            try:
                await self._pump_task
            except asyncio.CancelledError:
                # #4988's own gate (this coroutine IS running as a task,
                # scheduled via ``run_coroutine_threadsafe``): a bare
                # ``pass`` here would swallow BOTH this method's own
                # ``_pump_task.cancel()`` outcome AND an independent,
                # external cancellation of THIS coroutine's own task at
                # this exact await (e.g. a shutdown sweep) — checking
                # ``cancelling()`` before absorbing is session.py's own
                # #3377 precedent, not a new pattern.
                _this_task = asyncio.current_task()
                if _this_task is not None and _this_task.cancelling() > 0:
                    raise

    async def shutdown(self) -> None:
        await self._call_on_worker("shutdown")
        assert self._worker_loop is not None and self._thread is not None
        concurrent_future = asyncio.run_coroutine_threadsafe(
            self._cancel_pump_on_worker(), self._worker_loop,
        )
        await asyncio.wrap_future(concurrent_future)
        self._worker_loop.call_soon_threadsafe(self._worker_loop.stop)
        # ``Thread.join()`` blocks — off the caller's own loop via
        # ``asyncio.to_thread`` so ``shutdown()`` staying a coroutine never
        # reintroduces the exact "TUI does not get scheduled" symptom this
        # class exists to remove. It is also the REAL completion signal
        # here: the loop has genuinely stopped and the thread function has
        # returned once ``join()`` unblocks, not merely "a stop was asked for".
        await asyncio.to_thread(self._thread.join)


__all__ = ["ThreadedTransportProxy"]
