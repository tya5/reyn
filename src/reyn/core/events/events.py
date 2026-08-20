from __future__ import annotations

import asyncio
import contextvars
import inspect
import logging
import secrets
from datetime import date
from pathlib import Path
from typing import Awaitable, Callable, Union

from reyn.core.events.backend import EventBackend
from reyn.schemas.models import Event

logger = logging.getLogger(__name__)

# #4961 C: a subscriber may be sync (the pre-existing shape — AG-UI's
# put_nowait-based forwarder, the OTEL exporter) or async (A2A/MCP's own
# ``ensure_future``-wrapped callbacks stay unchanged, but the dispatch
# consumer itself can now genuinely `await` a subscriber that IS a
# coroutine function). See `_dispatch_consumer`'s own docstring for how
# this is detected.
Subscriber = Callable[[Event], Union[None, Awaitable[None]]]


# #1669: session-scoped ambient EventLog for the single LLM acompletion chokepoint.
# ``recorded_acompletion`` (reyn.llm.llm) is the one place ALL LLM calls funnel
# through (#1190 AST-guarded), but it receives no events sink (only budget /
# recorder). Threading one through its 9 call sites would be churn AND incomplete
# (judge / compaction / dogfood callers lack a sink). Instead the chat session /
# kernel runtime sets this ContextVar to its EventLog at creation; the chokepoint
# reads it and emits ``llm_request``. ContextVars copy into child asyncio tasks at
# spawn, so a set-before-the-run-loop propagates to every in-session LLM call.
# None (tests / dogfood / CLI, no active session) → the chokepoint skips the emit,
# mirroring the ``recorder=None`` graceful path.
_llm_request_event_log: contextvars.ContextVar["EventLog | None"] = contextvars.ContextVar(
    "reyn_llm_request_event_log", default=None,
)


def set_llm_request_event_log(log: "EventLog | None") -> contextvars.Token:
    """Set the ambient EventLog the LLM chokepoint emits ``llm_request`` to (#1669).

    Returns the token so a caller MAY reset to the prior value for a nested scope;
    the session / runtime set-at-creation sites do not reset (last-set-wins is the
    intended session-scoped lifetime — the active top-level run owns the sink)."""
    return _llm_request_event_log.set(log)


def get_llm_request_event_log() -> "EventLog | None":
    """Read the ambient EventLog for the LLM chokepoint (#1669); None when unset."""
    return _llm_request_event_log.get()


class EventLog:
    def __init__(
        self,
        subscribers: list[Subscriber] | None = None,
        *,
        agent_id: str | None = None,
        run_id: str | None = None,
        emitter: str | None = None,
        track_audit_seq: bool = True,
        backend: "EventBackend | None" = None,
    ) -> None:
        # #3868 PR-1: a folded derived state for `present`'s "was this ref
        # already read this session?" question (source.py's compute_ingested),
        # built incrementally in emit() instead of re-scanned from an
        # unbounded full-history list on every present call. Keyed on the
        # read's own `path`; "full" is STICKY — a later truncated read on the
        # same path never downgrades it, because the operator (or a prior
        # full read) already saw the whole thing. What changed is the GROWTH
        # CLASS this dict is subject to: O(distinct paths ever read), not
        # O(every event ever emitted) — see compute_ingested's docstring for
        # why that is still unbounded in principle but bounded by real work
        # (a file read + permission gate) rather than by talk. #3868 PR-3:
        # the unbounded `_events` full-history list this fold replaced is
        # gone — `all()`/`to_json()` (its only readers) retired in PR-2's
        # collect_events() migration first.
        self._ingested: dict[str, str] = {}
        self._subscribers: list[Subscriber] = list(subscribers or [])
        # FP-0016 Component E: agent_id is auto-injected into every event
        # payload when set. None preserves prior behaviour for callers
        # (= tests + emit_cli_event) that don't have a session identity.
        self._agent_id = agent_id
        # Issue #134: run_id is auto-injected into every event payload
        # when set, mirroring the agent_id pattern. The run that
        # emits the event is recorded so that subscribers (= forwarder /
        # TUI) can distinguish events from a parent agent turn versus a
        # sub-agent turn (which inherits the parent's subscriber list).
        self._run_id = run_id
        # #4496 PR-1 (architect's contract 3): every audit-event carries a
        # monotonic `audit_seq` per `emitter`, so a subscriber can detect a
        # gap (a dropped event) without needing delivery-order guarantees —
        # NOT the WAL's own `seq` (a different concept: WAL seq is the
        # recovery coordinate; audit_seq is purely an audit-continuity
        # witness — see events.md and CLAUDE.md's own warning against
        # reusing that name for a second thing).
        #
        # `emitter` identifies ONE execution of a session, not the session
        # itself (a session_id is stable across restarts — reusing it would
        # let two different process runs both emit `audit_seq` 1..N under
        # the same emitter, making a genuine gap indistinguishable from "a
        # new run started"). Measured directly, not assumed: the session's
        # own audit EventLog (session.py's `audit_events = EventLog(...)`)
        # passes `agent_id` only, `run_id` stays unset there — so `run_id`
        # is NOT a reliable per-execution identity for this specific
        # EventLog instance, and this can't just alias it. A fresh EventLog
        # is constructed exactly once per real process execution (never
        # reloaded/reused across a restart), so a random token minted HERE,
        # once, at construction, is already unique per execution BY
        # CONSTRUCTION — no dependency on any other field being populated.
        # `emitter=` lets a caller override this (e.g. `emit_cli_event`
        # passing the literal `"cli"` label for its one-off, non-
        # continuous events — see that function's own construction site).
        self._emitter = emitter if emitter is not None else secrets.token_hex(8)
        # CLI one-off events have no continuity to protect (one event, one
        # process, never a second call from the same EventLog instance) —
        # architect's own ruling: "a series of exactly one has no meaning
        # for gap-detection", so `track_audit_seq=False` omits the key
        # entirely rather than always stamping a meaningless `1`.
        self._track_audit_seq = track_audit_seq
        self._audit_seq = 0
        # #4496 PR-2: the WRITE-side backend (local disk / discard — see
        # `backend.py`'s module docstring for the full contract and why
        # this is NOT a subscriber). None preserves the pre-PR-2 shape
        # (no write side at all — callers that want persistence add an
        # EventStore as a plain subscriber, same as before this PR).
        self._backend = backend
        # #4961 C (owner-ruled, architect + lead-coder design): a HANDOFF,
        # not a buffer — no upper bound, no drop policy, no blocking put.
        # `emit()` (sync, a hot path — every op, every tool call) can never
        # `await`, so it always just pushes here unconditionally; nothing
        # about DISPATCH depends on whether a running loop exists yet
        # (verified directly: `asyncio.Queue().put_nowait(...)` works with
        # no running loop at all — this is not a design guess). The
        # subscriber loop moves to `_dispatch_consumer`, running on the
        # queue's CONSUMER side instead of inline inside `emit()` — this is
        # what isolates a raising OR slow subscriber from `emit()`'s
        # caller (an op/tool's own execution path). Real backpressure for
        # a genuinely slow subscriber lives at THAT subscriber's own sink
        # (a socket, a file) — never here: this queue only grows during a
        # stretch where one task keeps emitting without ever `await`-ing
        # anything else (cooperative scheduling hands control to the
        # consumer the moment the producer yields); constant growth is a
        # signal a subscriber itself is stuck, observed at THAT layer, not
        # solved by inventing a bound on this handoff.
        self._dispatch_queue: "asyncio.Queue[Event]" = asyncio.Queue()
        self._consumer_task: "asyncio.Task[None] | None" = None

    @property
    def subscribers(self) -> list[Subscriber]:
        return self._subscribers

    @property
    def ingested_path_count(self) -> int:
        """How many DISTINCT paths :meth:`compute_ingested`'s derived state
        currently tracks (#3868 PR-1) — the public witness for its growth
        class: this grows with the number of unique paths ever read, never
        with the number of events emitted (a non-read event, or a REPEAT
        read of an already-tracked path, leaves this unchanged). Exists so
        that claim is testable without reading the private ``_ingested``
        dict directly.
        """
        return len(self._ingested)

    @property
    def agent_id(self) -> str | None:
        """The agent_id this EventLog stamps onto emitted events (FP-0016 E).

        Public read-only view of the constructor-injected agent_id so
        downstream consumers (= kernel executors building OpContext) can
        pick it up without a separate threading parameter.
        """
        return self._agent_id

    @property
    def run_id(self) -> str | None:
        """The run_id this EventLog stamps onto emitted events (issue #134)."""
        return self._run_id

    @property
    def backend(self) -> "EventBackend | None":
        """The active write-side backend (#4496 PR-2), or None (no write
        side — pre-PR-2 shape). Public read-only view so a consumer
        (`reyn events` CLI) can call `declare_gaps()` without reaching into
        private state."""
        return self._backend

    @property
    def emitter(self) -> str:
        """The emitter label this EventLog stamps onto every emitted event
        (#4496 PR-1, contract 3) — auto-generated at construction when not
        explicitly given. Public read-only view, same rationale as
        ``agent_id``/``run_id`` above: lets a caller confirm identity
        without reaching into private state."""
        return self._emitter

    def add_subscriber(self, fn: Subscriber) -> None:
        self._subscribers.append(fn)

    async def drain(self) -> None:
        """#4961 C (architect finding): wait until every event pushed to
        the dispatch queue so far has been FULLY processed — not merely
        "the queue is currently empty" (a bare `await asyncio.sleep(0)`
        yields exactly once, which is only enough if at most one event
        was pending; N pending events need N yields, so a fixed single
        yield is a coincidental pass, not a guarantee).

        This closes a REAL production gap, not just a test-determinism
        one: an event emitted right before a process/session actually
        exits (e.g. `session_completed`, emitted at the tail of
        `Session.run()`'s own `finally` block) has no subscriber-visible
        effect unless something awaits the consumer catching up before
        the process is gone — a caller that cares about "did the LAST
        event actually reach transports/OTEL" must await this. Session's
        own shutdown path awaits it for exactly this reason (see
        `Session.run()`'s own comment at its call site).

        Implemented via ``asyncio.Queue.join()`` (waits until
        ``task_done()`` has been called for every item ``put()`` so
        far) — deterministic regardless of how many events are queued,
        and does not depend on `_dispatch_consumer` having started yet
        (if it hasn't — no loop was ever running — the queue is empty
        by construction, since ``emit()`` unconditionally pushes there
        first; ``join()`` returns immediately).

        #4965 (measured, not guessed): ``Queue.join()`` alone is only
        safe while the consumer OUTLIVES this wait. It does not — the
        caller that calls ``drain()`` from ITS OWN shutdown path (e.g.
        `Session.run()`'s own `finally`) can be reached by a generic,
        uncontrolled task-cancellation sweep (`asyncio.run()`'s /
        pytest-asyncio's own end-of-loop `_cancel_all_tasks`) that
        cancels EVERY pending task up front, including this EventLog's
        own `_consumer_task`, before `gather()`-ing them. The caller's
        own coroutine absorbs its first delivered cancellation at
        whatever earlier await it was suspended at and does not see a
        second one on a LATER await inside the same `finally` — so by
        the time it reaches `drain()`, the consumer may already be
        dead, and nothing will ever call `task_done()` again:
        `Queue.join()` would then hang forever. Confirmed directly (a
        bounded per-task cancel probe against
        `test_pipeline_is2_driver_session.py`'s leftover tasks) — this
        is not a hypothetical race.

        So this races the queue-join against the consumer task itself:
        if the consumer ends (cancelled, or any other reason) before
        the queue finishes draining, stop waiting on `join()` — it can
        no longer complete — and report what was left undelivered
        rather than hang OR silently pretend everything was flushed.
        """
        self._ensure_consumer_started()
        consumer = self._consumer_task
        join_future = asyncio.ensure_future(self._dispatch_queue.join())
        if consumer is None:
            # No running loop was ever available to start a consumer —
            # nothing will ever pull from the queue via that path, but
            # `_ensure_consumer_started` swallowing RuntimeError means we
            # are not inside a running loop either, so `drain()` itself
            # could not have been awaited here. Kept for defensive
            # symmetry with `_ensure_consumer_started`'s own contract.
            await join_future
            return
        done, _ = await asyncio.wait(
            {join_future, consumer}, return_when=asyncio.FIRST_COMPLETED
        )
        if join_future in done:
            return
        # The consumer ended first — draining can never complete now.
        # Stop waiting on the now-orphaned join() rather than hang.
        join_future.cancel()
        try:
            await join_future
        except asyncio.CancelledError:
            pass
        remaining = self._dispatch_queue.qsize()
        if remaining:
            logger.warning(
                "EventLog.drain(): consumer task ended (cancelled or "
                "otherwise) before the dispatch queue finished draining "
                "— %d event(s) (emitter=%s) were never delivered to live "
                "subscribers (transport/OTEL). The audit record itself is "
                "NOT lost: emit() writes to `self._backend` (.reyn/events) "
                "before ever queueing for dispatch, so these %d event(s) "
                "are already durably recorded — only live delivery was "
                "skipped.",
                remaining, self._emitter, remaining,
            )

    async def stop_dispatch(self) -> None:
        """#4961 C (architect ruling): the stop half of the start/stop
        pair — whoever's code path started the consumer (via `emit()` or
        `drain()`) also has a way to STOP it, deterministically, rather
        than leaving an unbounded `while True` task for something ELSE
        to eventually clean up.

        Call ``drain()`` BEFORE this — draining flushes everything
        already queued; stopping after ensures nothing new can be queued
        without a consumer to pick it up. The pair must run in THIS
        order (drain, then stop) and must complete before whatever owns
        this EventLog's lifetime hands control to a generic task-
        cancelling shutdown path (e.g. `asyncio.run()`'s / pytest-
        asyncio's own end-of-loop `_cancel_all_tasks`) — that path is
        outside our control and gathers EVERY still-pending task with no
        ordering guarantee, so anything that depended on THIS consumer
        having delivered an event first (a caller that used to observe
        synchronous dispatch, pre-#4961 C) can hang waiting for a
        delivery that will now never happen once the consumer is
        cancelled out from under it. Closing explicitly, in the owner's
        own code, before that point is the only way to avoid landing
        there at all.

        No-op if the consumer was never started (nothing to stop).
        """
        if self._consumer_task is None:
            return
        task = self._consumer_task
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        self._consumer_task = None

    def set_backend(self, backend: "EventBackend | None") -> None:
        """Swap the WRITE-side backend (#4496 PR-2) — e.g. re-pointing to a
        new `EventStore`-backed `LocalEventBackend` after `set_events_dir`
        re-keys a spawned session's events directory. Deliberately a plain
        setter, not add/remove: exactly one backend is active at a time
        (unlike subscribers, which are a list) — the PREVIOUS backend is
        simply dropped, no explicit "remove" step needed."""
        self._backend = backend

    def remove_subscriber(self, fn: Subscriber) -> bool:
        """Detach a previously added subscriber.

        Returns True iff the subscriber was found and removed. Used by
        scoped consumers that subscribe for the duration of one call
        (= e.g. issue #271 M1 MCP progress bridge: subscribe in
        ``_call_tool``, unsubscribe in ``finally``) so the subscriber
        list doesn't grow unboundedly across many calls.
        """
        try:
            self._subscribers.remove(fn)
            return True
        except ValueError:
            return False

    def emit(self, type: str, **data) -> Event:
        # FP-0016 Component E: stamp the session's agent_id onto every
        # event payload so the P6 audit trail can answer "which agent
        # did this?" without correlating across multiple logs.  Caller-
        # provided ``agent_id`` wins (= delegation flows may preserve
        # the upstream origin's identity).
        if self._agent_id and "agent_id" not in data:
            data = {**data, "agent_id": self._agent_id}
        # Issue #134: stamp run_id with the same caller-wins convention
        # as agent_id. Lets subscribers route events to the correct
        # row when a child agent shares the parent's subscriber list.
        if self._run_id and "run_id" not in data:
            data = {**data, "run_id": self._run_id}
        # #4496 PR-1: emitter + audit_seq are ALWAYS this EventLog's own —
        # deliberately NOT caller-wins (unlike agent_id/run_id above).
        # Letting a caller supply either would open a path to skip a
        # number or forge one, which would make a real gap indistinguishable
        # from an intentional skip — the exact failure mode contract 3
        # exists to prevent. Overwrites any caller-supplied same-named key
        # rather than merely defaulting it.
        data = {**data, "emitter": self._emitter}
        if self._track_audit_seq:
            self._audit_seq += 1
            data["audit_seq"] = self._audit_seq
        event = Event(type=type, data=data)
        # #3868 PR-1: fold this event's contribution to `_ingested` at emit
        # time (a dict update) instead of re-scanning full history at every
        # `present` call (was O(session length) per call, source.py:154).
        # Early-return on the common case (not a read) first — `emit` is a
        # hot path (every op, every tool call) and this only has work to do
        # for a specific op kind.
        if type == "tool_executed":
            op = data.get("op")
            if op in ("read_file", "read"):
                path = data.get("path")
                if path is not None:
                    if data.get("truncated"):
                        # Sticky full: a later partial read on a path already
                        # seen in full does not downgrade it — the operator
                        # (or a prior read) already has the whole thing.
                        if self._ingested.get(path) != "full":
                            self._ingested[path] = "partial"
                    else:
                        self._ingested[path] = "full"
        # #4496 PR-2: the backend writes BEFORE the subscriber loop runs,
        # and its own exception is caught right here — never let a backend
        # failure (e.g. a future network backend's connection error) reach
        # a subscriber, and (by running first) never let a raising
        # subscriber prevent the backend from having already written. See
        # `backend.py`'s module docstring for the full "not a subscriber"
        # rationale.
        if self._backend is not None:
            try:
                self._backend.write(event)
            except Exception:
                logger.exception(
                    "event backend write failed (emitter=%s type=%s) — "
                    "continuing to subscriber dispatch",
                    self._emitter, type,
                )
        # #4961 C: hand off to the dispatch queue instead of running the
        # subscriber loop inline — `emit()` always just pushes, unconditionally
        # (no branch on whether a running loop exists yet; see the queue's
        # own declaration comment above for why that is deliberate and
        # measured, not a guess). `_dispatch_consumer` runs the actual
        # per-subscriber loop on the queue's CONSUMER side.
        self._dispatch_queue.put_nowait(event)
        # Opportunistically ensure a consumer is running. Idempotent (only
        # the FIRST successful call spawns a task; every later `emit()`
        # sees `_consumer_task` already set and no-ops here) and confined
        # to bootstrapping — it does not change what gets dispatched or
        # how, only whether something is currently draining the queue.
        # `RuntimeError` (no running loop yet, e.g. `emit_cli_event` calls
        # made before any `asyncio.run()`) is swallowed the same way
        # AG-UI/A2A/MCP's own `ensure_future` call sites already do:
        # events accumulate in the queue and are drained by whichever
        # LATER `emit()` call finally runs inside a live loop. A process
        # that never starts a loop at all never gets a consumer — its
        # subscribers (all of them transport/OTEL, all loop-dependent)
        # were never going to fire anyway; `self._backend.write` above
        # already ran, so the audit record itself is not lost, only live
        # subscriber delivery.
        self._ensure_consumer_started()
        return event

    def _ensure_consumer_started(self) -> None:
        """Idempotent bootstrap shared by ``emit()`` and ``drain()`` — only
        the FIRST successful call spawns ``_dispatch_consumer``; every
        later call sees ``_consumer_task`` already set and no-ops.
        ``RuntimeError`` (no running loop yet, e.g. ``emit_cli_event``
        calls made before any ``asyncio.run()``) is swallowed the same
        way AG-UI/A2A/MCP's own ``ensure_future`` call sites already do:
        events accumulate in the queue and are drained once a LATER call
        (from either method) finally runs inside a live loop. A process
        that never starts a loop at all never gets a consumer — its
        subscribers (all of them transport/OTEL, all loop-dependent)
        were never going to fire anyway; ``self._backend.write`` in
        ``emit()`` already ran by the time this is reached, so the audit
        record itself is not lost, only live subscriber delivery.
        ``drain()`` needs this too, not just ``emit()``: a caller could
        reach ``drain()`` in a loop where no ``emit()`` from THIS
        EventLog has run yet (e.g. draining right at session start),
        and without a consumer running, ``queue.join()`` would wait
        forever for ``task_done()`` calls nothing will ever make.
        """
        if self._consumer_task is None:
            try:
                self._consumer_task = asyncio.ensure_future(self._dispatch_consumer())
            except RuntimeError:
                pass

    async def _dispatch_consumer(self) -> None:
        """#4961 C: runs for the lifetime of the process's event loop,
        draining ``_dispatch_queue`` and dispatching each event to
        ``self._subscribers`` — the subscriber loop moved OFF of `emit()`'s
        synchronous caller and onto here.

        Per-subscriber isolation (#4961 A) is preserved unchanged: a
        raising subscriber is logged and does not stop the next one.
        Iterates the LIVE ``self._subscribers`` list (not a snapshot) —
        same semantics `emit()`'s own inline loop always had; a subscriber
        added mid-drain is picked up by the loop's next iteration of
        ``for sub in self._subscribers``, same as it always could be.

        A subscriber may be sync (``Subscriber``, e.g. AG-UI's
        ``put_nowait``-based forwarder or the OTEL exporter) or async
        (``Callable[[Event], Awaitable[None]]``). Detected by calling it
        and checking whether the RESULT is awaitable
        (``inspect.isawaitable``, not ``inspect.iscoroutinefunction`` on
        the callable itself — architect finding: the callable-level check
        misses a ``functools.partial``, a callable object's ``__call__``,
        or a decorator-wrapped function, all of which can report
        non-coroutine while still returning an awaitable when called).
        """
        while True:
            event = await self._dispatch_queue.get()
            try:
                for sub in self._subscribers:
                    try:
                        result = sub(event)
                        if inspect.isawaitable(result):
                            await result
                    except Exception:
                        logger.exception(
                            "event subscriber failed (emitter=%s type=%s) — "
                            "continuing to the next subscriber",
                            self._emitter, event.type,
                        )
            finally:
                # #4961 C (architect finding): pairs with `drain()` below
                # via ``asyncio.Queue.join()`` — without this, nothing can
                # deterministically tell "the queue is empty" apart from
                # "the queue is empty AND the item currently being
                # dispatched has actually finished". In `finally` so a
                # completely unexpected exception here still lets a
                # waiting `drain()` proceed rather than hang forever.
                self._dispatch_queue.task_done()

    def compute_ingested(self, data_ref: str, resolved: str) -> str:
        """``ingested`` ∈ ``{none, partial, full}`` for a present ``data_ref``
        (#3868 PR-1) — an O(1) lookup into the state :meth:`emit` folds
        incrementally, replacing source.py's former O(session length) scan
        over the full event history.

        Checked under BOTH keys a caller might resolve a ref by (the raw
        ``data_ref`` and its ``resolved`` form — source.py's own pre-existing
        two-key check, unchanged here), with ``full`` winning if the two keys
        disagree.

        Blindness is an audit annotation, not a permission mode: this
        reports whether a prior ``read_file`` on this ref appears earlier in
        the session — never LLM-self-reported.

        Still unbounded in principle (read enough distinct paths and this
        dict grows without limit) — NOT bounded to a fixed size, deliberately:
        a ``deque(maxlen=N)``-style cap would make an old path's entry
        silently vanish, and a caller re-presenting that ref would then see
        ``none`` instead of ``full`` — a false "you haven't read this yet"
        for a ref that WAS fully read, which is worse than the unbounded
        growth it would avoid. What bounds it in practice is that every
        entry costs a real ``file.read`` (I/O + the permission gate) to
        create — growth is bounded by actual work done, not by how much an
        agent can emit.
        """
        a = self._ingested.get(data_ref)
        b = self._ingested.get(resolved)
        if a == "full" or b == "full":
            return "full"
        if a == "partial" or b == "partial":
            return "partial"
        return "none"


def _find_reyn_dir(start: Path) -> Path | None:
    """Walk up from *start* until finding a directory containing `.reyn/`, or return None."""
    current = start.resolve()
    while True:
        candidate = current / ".reyn"
        if candidate.is_dir():
            return candidate
        parent = current.parent
        if parent == current:
            return None
        current = parent


def emit_cli_event(kind: str, **payload) -> None:
    """Emit a one-off P6 event from a CLI context (no active session).

    Routes to ``.reyn/events/direct/cli/<YYYY-MM-DD>.jsonl``. Locates the
    ``.reyn/`` dir by walking up from ``Path.cwd()``. If no ``.reyn/``
    directory is found, logs a warning and returns silently — the caller's
    operation is the primary action; audit-emit failure must not propagate.

    The file is appended to (P6 append-only contract). Dir creation is
    idempotent (``mkdir(parents=True, exist_ok=True)``).
    """
    from reyn.core.events.event_store import EventStore

    reyn_dir = _find_reyn_dir(Path.cwd())
    if reyn_dir is None:
        logger.warning(
            "emit_cli_event: no .reyn/ directory found from %s; "
            "skipping P6 audit emit for event %r",
            Path.cwd(),
            kind,
        )
        return

    cli_dir = reyn_dir / "events" / "direct" / "cli"
    today = date.today().isoformat()  # YYYY-MM-DD
    # Use a date-named suffix so each day's CLI events land in one predictable file.
    # max_bytes=0 / max_age_seconds=0 disables rotation — the suffix IS the date.
    store = EventStore(cli_dir, max_bytes=0, max_age_seconds=0, suffix=f"_{today}")
    # #4496 PR-1: a one-off CLI event has no continuity to protect — a
    # single event from a single process is not a series a gap can be
    # detected in, so audit_seq is omitted entirely (architect's ruling)
    # rather than always stamping a meaningless `1`. `emitter="cli"` is a
    # legible label (not a random per-instance token — nothing needs to
    # distinguish one CLI invocation's emitter from another's, since
    # neither carries a sequence to compare).
    event_log = EventLog(subscribers=[store], emitter="cli", track_audit_seq=False)
    event_log.emit(kind, **payload)
