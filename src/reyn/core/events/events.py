from __future__ import annotations

import asyncio
import contextvars
import inspect
import logging
import secrets
from collections.abc import Iterable
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
        _force_inline: bool = False,
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
        # #5260: declared interest, per subscriber. Absent = every event (the
        # pre-#5260 contract). Kept beside the list rather than inside it so
        # ``subscribers`` stays a list of callables for its existing readers.
        self._subscriber_kinds: "dict[Subscriber, frozenset[str]]" = {}
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
        # `await`. When a running loop exists, `emit()` pushes here and the
        # subscriber loop runs on the queue's CONSUMER side (`_dispatch_
        # consumer`) instead of inline — this is what isolates a raising OR
        # slow subscriber from `emit()`'s caller (an op/tool's own
        # execution path). Real backpressure for a genuinely slow
        # subscriber lives at THAT subscriber's own sink (a socket, a
        # file) — never here: this queue only grows during a stretch where
        # one task keeps emitting without ever `await`-ing anything else
        # (cooperative scheduling hands control to the consumer the moment
        # the producer yields); constant growth is a signal a subscriber
        # itself is stuck, observed at THAT layer, not solved by inventing
        # a bound on this handoff.
        #
        # #4966 (architect ruling, reversing an earlier "always push
        # unconditionally" design): when NO running loop exists, `emit()`
        # dispatches INLINE instead — see `emit()`'s own comment for why.
        # This queue and its consumer task are simply unused in that case.
        self._dispatch_queue: "asyncio.Queue[Event]" = asyncio.Queue()
        self._consumer_task: "asyncio.Task[None] | None" = None
        # #4966 (architect ruling — a DIFFERENT concern from the queue-vs-
        # inline invariant above, not another instance of it): that
        # invariant is about whether async delivery CAN happen (loop
        # present/absent, consumer cancelled) — the mechanism itself can
        # judge that. This flag is about whether anyone OWNS this
        # EventLog well enough to ever call `drain()`/`stop_dispatch()` on
        # it — construction-time information the mechanism cannot infer
        # (an owner can appear later; guessing "unowned" and dispatching
        # async anyway fails SILENTLY when the guess is wrong, the exact
        # shape this arc kept re-finding). So this is a declaration, not
        # an inference — and a private, single-purpose one: NOT a public
        # constructor parameter (that would let any caller re-introduce
        # the queue/consumer coupling #4961 C was built to remove).
        # Bounded to exactly ONE legitimate call site, `emit_cli_event`'s
        # own one-off, no-continuity EventLog (see its construction site)
        # — enforced by a test that enumerates every `_force_inline=True`
        # call site and fails if a second one ever appears, not a gate
        # (one site is a single fact a test can pin, not a population a
        # gate needs to sweep for).
        #
        # Without it: `emit_cli_event`'s throwaway EventLog never gets
        # `drain()`/`stop_dispatch()` (nothing holds a reference after its
        # one `emit()` call returns), so if a loop happened to be running
        # around that call site, its spawned consumer task would be
        # silently abandoned — and when the loop eventually tears down,
        # asyncio reports the abandoned task as a SECOND, spurious "Task
        # was destroyed but it is pending!" unhandled-exception context,
        # alongside whatever real exception a caller's own diagnostics
        # were trying to witness (found via CI:
        # test_durable_capture_survives_prompt_toolkit_prompt_wait).
        self._force_inline = _force_inline

    @property
    def subscribers(self) -> list[Subscriber]:
        """A read-only SNAPSHOT of the current subscriber list.

        #4966 (lead-coder finding): returns a copy, not ``self._subscribers``
        itself — this property used to return the LIVE list, meaning
        ``log.subscribers.append(fn)`` was a THIRD, undocumented way to
        become a subscriber (alongside the two intended ones,
        ``add_subscriber()`` and the ``subscribers=[...]`` constructor
        argument), bypassing whatever bookkeeping either of those two
        might someday need to do. Measured to have zero current callers
        of that pattern, so closing it here breaks nothing — after this,
        ``add_subscriber()``/the constructor argument are the ONLY two
        ways in, making the population of "how does something become a
        subscriber" closed by construction rather than an open set a
        future census has to keep re-discovering.

        A plain ``list(...)`` copy, not a ``tuple`` — existing callers
        compare this against a list literal (``assert log.subscribers ==
        [collected.append]``), and ``list == tuple`` is always ``False``
        in Python regardless of contents."""
        return list(self._subscribers)

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

    def add_subscriber(self, fn: Subscriber, *, kinds: "Iterable[str] | None" = None) -> None:
        """Register *fn*; with ``kinds``, only for those event types (#5260).

        Every subscriber used to be called for every event and filter itself on
        the way in — the same decision written out at each of them, and a cost
        paid per event per subscriber for the ones that only ever wanted a few
        kinds. Declaring it here moves the decision to where the subscriber is
        registered, and lets the dispatcher skip instead of the subscriber
        returning.

        ``kinds=None`` keeps the pre-#5260 contract exactly: every event. A
        subscriber whose interest is dynamic (computed per event, not fixed at
        registration) must keep filtering itself and pass nothing here — the
        declaration is an optimisation of a FIXED interest, and claiming a fixed
        one that is not fixed drops events silently.
        """
        self._subscribers.append(fn)
        if kinds is not None:
            self._subscriber_kinds[fn] = frozenset(kinds)

    def _wants(self, sub: Subscriber, event: "Event") -> bool:
        """Whether *sub* declared an interest that excludes this event (#5260)."""
        declared = self._subscriber_kinds.get(sub)
        return declared is None or event.type in declared

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
            # #4986: `await join_future` raises CancelledError for TWO
            # possible reasons, indistinguishable by the exception alone —
            # (a) `join_future.cancel()` two lines up, its own outcome,
            # exactly what this except exists to absorb; (b) THIS
            # coroutine's OWN task was independently, externally cancelled
            # (e.g. pytest-asyncio's/`asyncio.run()`'s end-of-loop
            # `_cancel_all_tasks`, which cancels every task in
            # `asyncio.all_tasks()` — including whatever task is running
            # `drain()` right now — with no ordering guarantee relative to
            # this await). Swallowing unconditionally used to treat both
            # the same: (b) would silently continue past this method
            # (logging a "some events undelivered" warning, then
            # RETURNING NORMALLY) instead of propagating — the exact
            # cancel-swallow session.py's own #3377 precedent
            # (`_driver.cancelling() > 0`) already exists to prevent, not
            # applied here. `Task.cancelling()` (Python 3.11+, this repo's
            # own floor — pyproject.toml `requires-python = ">=3.11"`)
            # answers which case this is: >0 means a cancellation request
            # against THIS task is still outstanding (case (b)) and must
            # be re-raised so the caller's own teardown/shutdown actually
            # happens instead of appearing to complete; 0 means this
            # task's own cancel count nets to zero, so the CancelledError
            # just seen can only have come from `join_future` itself
            # (case (a)) and is safe to absorb, unchanged from before.
            _current = asyncio.current_task()
            if _current is not None and _current.cancelling() > 0:
                raise
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

        #4966 (architect finding): "stop" here is NOT "never again" — a
        LATER `emit()`/`drain()` call on this same EventLog (e.g. a fresh
        `asyncio.run()` reusing an instance that outlived the loop this
        consumer was stopped on) spawns a NEW consumer task the normal
        way (`_ensure_consumer_started`), same as if none had ever run.
        This is not a gap in what stopping promises: the guarantee is
        "nothing accumulates without a consumer to pick it up", not "this
        instance never dispatches again" — a later `emit()` getting a
        fresh consumer satisfies that guarantee just as well as the first
        one did. Documented here because the shape reads, out of context,
        like a task that should have stayed stopped came back — it did
        not come back; it is a different task, started by the same
        idempotent bootstrap every `emit()`/`drain()` call already goes
        through.
        """
        if self._consumer_task is None:
            return
        task = self._consumer_task
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            # #4986: same ambiguity as `drain()`'s own identical shape a
            # few lines up this file (see that except block's own
            # docstring-length comment for the full reasoning) —
            # `await task` raising CancelledError here could be `task`'s
            # own cancellation outcome (this method's own `task.cancel()`
            # two lines up) OR THIS coroutine's own task being
            # independently, externally cancelled at the same await. The
            # unconditional `pass` this replaces could not tell them
            # apart, so an external cancel landing here used to be
            # silently absorbed — `self._consumer_task = None` would run
            # and this method would return normally, instead of the
            # caller's own cancellation actually propagating. Checked the
            # same way session.py's own #3377 precedent
            # (`_driver.cancelling() > 0`) already does.
            _current = asyncio.current_task()
            if _current is not None and _current.cancelling() > 0:
                raise
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
        except ValueError:
            return False
        # #5260: the declaration goes with it. This method exists so the
        # subscriber list does not grow unboundedly across many scoped calls
        # (the docstring above says so); a declaration dict that kept an entry
        # per departed subscriber would grow at exactly the rate this bounds —
        # and each entry holds the callable, so a closure's session would go
        # with it.
        self._subscriber_kinds.pop(fn, None)
        return True

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
        # #4966 (architect ruling, reversing #4961 C's original "always
        # push unconditionally" design): branch on whether a running loop
        # exists RIGHT NOW. Loop present → hand off to the dispatch queue
        # (unchanged #4961 C design: isolates emit()'s caller from a
        # raising OR slow subscriber). No loop → dispatch INLINE, right
        # here, synchronously.
        #
        # The original design swallowed `RuntimeError` from
        # `asyncio.ensure_future` in a no-loop context and just queued the
        # event, on the reasoning that "no loop ever runs" implies "no
        # loop-dependent subscriber (transport/OTEL) was ever going to
        # fire anyway." That reasoning is what architect found wrong: it
        # protected "dispatch timing is predictable" (queue vs inline) by
        # sacrificing "dispatch HAPPENING is predictable" — a no-loop
        # caller's subscribers (however many, sync-only by construction
        # since nothing here can await them) silently NEVER fired, not
        # merely later. That is a correctness regression for any
        # subscriber added in a no-loop context — not just the known CLI
        # edge, but also every fully-synchronous test that predates
        # #4961 C and used to observe inline, same-call dispatch.
        #
        # The inline branch reuses #4963's own per-subscriber isolation
        # unchanged (try/except per subscriber, one failure never blocks
        # the next) — the property that must hold ("delivery happens",
        # "one failure doesn't cascade", "a subscriber's own exception
        # never reaches emit()'s caller") holds identically in both
        # branches. The only difference between the branches is WHEN
        # dispatch happens, and a no-loop context has no op/tool caller to
        # protect from a slow subscriber and no transport to hand off to
        # asynchronously — so that difference carries no meaning there.
        if self._force_inline:
            self._dispatch_inline(event)
            return event
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            self._dispatch_inline(event)
        else:
            # #4966 (found via CI): `_ensure_consumer_started()` must run
            # BEFORE `put_nowait`, not after — it may need to replace
            # `_dispatch_queue` itself with a fresh one (see that method's
            # own docstring for why a stale consumer implies a stale
            # queue too, both bound to the same dead loop). Putting into
            # the OLD queue first and only then discovering it needs
            # replacing would silently drop that event.
            self._ensure_consumer_started()
            self._dispatch_queue.put_nowait(event)
        return event

    def _dispatch_inline(self, event: Event) -> None:
        """#4966: the no-running-loop half of `emit()`'s dispatch branch —
        run the per-subscriber loop synchronously, right here, instead of
        queueing for `_dispatch_consumer`. Mirrors #4963's per-subscriber
        isolation exactly (a raising subscriber is logged and does not
        stop the next one) — see `_dispatch_consumer`'s own docstring for
        the twin implementation this must stay in sync with.

        A subscriber that returns an awaitable (an async subscriber)
        cannot be awaited here — there is no running loop to await it on.
        Logged as a warning rather than silently dropped or raised: this
        is a real, disclosed gap (that subscriber's async work does not
        run for this event), not a crash and not a silent loss — every
        no-loop subscriber observed in this codebase so far (CLI's
        EventStore, AG-UI's put_nowait forwarder) is sync-only by
        construction, so this path is not expected to fire in practice.
        """
        for sub in self._subscribers:
            if not self._wants(sub, event):
                continue
            try:
                result = sub(event)
                if inspect.isawaitable(result):
                    logger.warning(
                        "event subscriber %r returned an awaitable but "
                        "emit() is dispatching inline (no running event "
                        "loop) — cannot await it here; that subscriber's "
                        "async work will not run for this event "
                        "(emitter=%s type=%s)",
                        sub, self._emitter, event.type,
                    )
            except Exception:
                logger.exception(
                    "event subscriber failed (emitter=%s type=%s) — "
                    "continuing to the next subscriber",
                    self._emitter, event.type,
                )

    def _ensure_consumer_started(self) -> None:
        """Idempotent bootstrap shared by ``emit()`` and ``drain()`` — only
        the FIRST successful call spawns ``_dispatch_consumer``; every
        later call sees ``_consumer_task`` already set and no-ops.

        #4966: no longer swallows ``RuntimeError`` — both call sites now
        only reach this method when a running loop is already confirmed
        (``emit()``'s own branch checks first; ``drain()`` is itself
        ``async def``, so a caller can only reach it from inside a
        running loop). ``asyncio.ensure_future`` is therefore expected to
        always succeed here; swallowing its ``RuntimeError`` used to be
        the source of a no-loop caller's events silently never being
        dispatched (see ``emit()``'s own comment on the branch this
        replaced).

        #4966 (found via CI, mechanism ruling — this is a DIFFERENT
        question from ``_force_inline``'s, and answered the opposite way
        deliberately): a caller can drive the SAME EventLog through
        multiple, SEPARATE ``asyncio.run()`` calls (each opening and
        closing its own loop). ``_consumer_task`` only resets to ``None``
        inside ``stop_dispatch()`` — nothing calls that between one
        ``asyncio.run()`` and the next, so after the first loop closes,
        ``self._consumer_task`` still holds a reference to a task bound
        to a now-DEAD loop. A bare ``is None`` check treats that stale
        reference as "already running" and never spawns a fresh consumer
        for the second loop — events emitted there queue forever with
        nobody draining them (found via CI:
        test_catalog_search_actions_emits_complete_on_query_failure,
        which drives 4 separate ``asyncio.run()`` calls through one
        EventLog).

        Unlike ``_force_inline`` (a declaration, because "will an owner
        ever appear" is unknowable at construction time — see its own
        comment), "is the existing consumer task still alive on a live
        loop" IS knowable by asking: ``task.done()`` is true once a task
        has finished (including via cancellation), and a task whose own
        loop has closed can never make further progress regardless of
        ``done()``'s current value. Respawn whenever the existing
        reference is stale by either measure — the mechanism can judge
        this because it queries a fact, not because it guesses one.

        A stale consumer implies a stale ``_dispatch_queue`` too:
        ``asyncio.Queue`` binds to whichever loop first calls one of its
        async methods (``.get()``), and raises ``RuntimeError: ... is
        bound to a different event loop`` if a later call arrives from a
        DIFFERENT running loop — exactly what a fresh consumer's first
        ``await self._dispatch_queue.get()`` would hit if the queue
        itself weren't also replaced here. Discarding whatever was
        already queued at staleness-detection time is not new loss on
        top of what already happened: WHEN the old consumer ended via
        cancellation (the common shutdown path — its loop's own teardown
        cancelling it), its own ``CancelledError`` handler already
        flushed whatever was pending before that loop closed (the same
        inline flush ``_dispatch_consumer``'s own docstring describes).
        If instead it ended via an uncaught exception escaping ``get()``/
        ``task_done()`` (outside the per-subscriber ``try/except``,
        which only isolates a SUBSCRIBER's own failure) — a rare,
        near-unreachable path — that flush does not run and whatever was
        still queued IS lost at that point, before this method ever
        runs. Either way, only LIVE subscriber notification is at risk
        here: ``self._backend.write`` in ``emit()`` already ran, before
        the queue push, for every one of those events — the durable
        audit record is intact regardless of which exit path the old
        consumer took.
        """
        task = self._consumer_task
        if task is None or task.done() or task.get_loop().is_closed():
            self._dispatch_queue = asyncio.Queue()
            self._consumer_task = asyncio.ensure_future(self._dispatch_consumer())

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

        #4966 (architect ruling, a SECOND application of the same
        invariant ``emit()``'s own no-loop branch already applies): the
        dispatch queue is an optimization for when delivery CAN happen
        asynchronously — never the CONDITION for delivery happening at
        all. ``emit()`` already applies this when no loop exists yet (at
        emit time); this task's own ``CancelledError`` handler applies it
        a second time when the loop is ENDING (at dispatch time) — a
        caller wrapped in ``asyncio.run(coro)`` closes the loop the
        moment ``coro`` returns, and that shutdown cancels every still-
        running task (this one included) BEFORE gathering them; without
        this handler, whatever is still queued at that instant is lost
        forever, not merely delayed — the same "permanent, not delayed"
        failure class #4965 named for ``drain()`` itself, but on the
        OPPOSITE side (this consumer dying, not ``drain()``'s own wait
        being orphaned — the two are complementary, not overlapping,
        fixes). On ``CancelledError``, whatever remains in the queue is
        flushed SYNCHRONOUSLY (via ``_dispatch_inline``, reusing #4963's
        per-subscriber isolation unchanged) before re-raising to let
        cancellation actually propagate — this closes the "loop closes ->
        consumer dies before draining" class identically regardless of
        WHO cancelled (``asyncio.run()``'s own teardown, pytest-asyncio's
        ``_cancel_all_tasks``, or this class's own ``stop_dispatch()``),
        with no test-level ``settle()`` call needed for this specific
        class — see #4966's PR body for the ①②③ split this closes ③ of.

        Constraint: this flush path can only run subscribers SYNCHRONOUSLY
        (no running loop survives long enough after cancellation to await
        an async subscriber's result here) — every subscriber in this
        codebase today is sync-only by construction (A2A/MCP's own
        callbacks return immediately via their own ``ensure_future``), so
        this is not a current gap, but it IS a real constraint on any
        FUTURE async subscriber: its async work will not run for an event
        flushed on this path. If that ever matters, this is the place to
        revisit, not `_dispatch_inline`'s own identical constraint (kept
        in sync with it deliberately — see that method's own docstring).
        """
        try:
            while True:
                event = await self._dispatch_queue.get()
                try:
                    for sub in self._subscribers:
                        if not self._wants(sub, event):
                            continue
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
                    self._dispatch_queue.task_done()
        except asyncio.CancelledError:
            while not self._dispatch_queue.empty():
                pending_event = self._dispatch_queue.get_nowait()
                self._dispatch_inline(pending_event)
                self._dispatch_queue.task_done()
            raise

    def flush_agent_delta(self, chain_id: str) -> None:
        """#4960 — the terminal-flush half of the ``agent_delta`` durability
        guarantee: call once a streaming chain ends (success, exception, or
        cancellation — see ``RouterLoop.run()``'s own ``finally``), so any
        fragments coalesced-but-not-yet-persisted for *chain_id* get one
        final durable record instead of silently vanishing.

        A passthrough to the backend, not a new mechanism of its own — only
        ``LocalEventBackend`` implements ``flush_pending_deltas`` (the
        coalescing is entirely a write-side, backend-specific concept, see
        ``backend.py``'s own docstring); any OTHER backend (``discard``, or
        a future one) silently no-ops here, matching how every other
        backend-specific behavior in this module degrades. Never raises —
        same "log, don't propagate" posture ``emit()`` already gives the
        backend's own ``write()`` failures."""
        flush_fn = getattr(self._backend, "flush_pending_deltas", None)
        if flush_fn is None:
            return
        try:
            flush_fn(chain_id)
        except Exception:
            logger.exception(
                "event backend flush_pending_deltas failed (emitter=%s "
                "chain_id=%s) — pending agent_delta fragments for this "
                "chain may be lost",
                self._emitter, chain_id,
            )

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


def emit_direct_event(
    kind: str,
    *,
    surface: str,
    reyn_root: Path,
    track_audit_seq: bool = True,
    **payload,
) -> None:
    """Emit a one-off P6 audit event from a context with no live ``Session``
    (#5065: generalizes the #4496 CLI-only seam below — a REST/web mutation
    has the same "no Session to call ``emit_audit_event`` through" shape a
    CLI command does; ``.reyn/events`` is a workspace-plane concern
    (band: workspace-SSoT), and ``Session`` is a convenience layered on top
    of it, not its owner).

    Routes to ``<reyn_root>/events/direct/<surface>/<YYYY-MM-DD>.jsonl``.
    ``reyn_root`` (the ``.reyn/`` dir itself) is supplied by the caller —
    this function does no cwd-based discovery of its own. A long-lived
    server process's cwd is not reliably its project root (the same
    cwd-anchor hazard ``permissions.py`` already fixed once, #2415); a
    caller that has a resolved project root (e.g. a FastAPI route's
    ``get_project_root`` dependency) must pass it through explicitly
    rather than let this function re-derive it from ``Path.cwd()``.

    ``track_audit_seq`` defaults to ``True`` (a series assumption) — pass
    ``False`` only for a genuinely one-shot-per-process caller. #4496's
    "omit audit_seq, a single event isn't a series a gap can be detected
    in" ruling was scoped to that one-shot CLI shape specifically; it does
    not generalize to a long-lived surface (e.g. a web server) that emits
    many events from the same process — those DO form a series.

    The file is appended to (P6 append-only contract). Dir creation is
    idempotent (``mkdir(parents=True, exist_ok=True)``). If ``reyn_root``
    does not exist, logs a warning and returns silently — the caller's
    operation is the primary action; audit-emit failure must not
    propagate. This makes the guarantee best-effort, not just here but at
    the underlying ``EventStore.write`` too (its own per-subscriber
    isolation logs and swallows rather than raises) — a caller's primary
    write can succeed while its audit record silently does not; there is
    no separate signal distinguishing that case from "nothing happened."

    ``surface`` is stamped into the emitted event's own payload (a
    ``"surface"`` field, deterministic — a caller-supplied ``surface`` key
    in ``**payload`` is overwritten, never merged), not just used for the
    ``EventLog``'s ``emitter`` label — the latter is not treated as a
    per-kind schema field by the census/vocabulary tooling, so a kind that
    declares ``surface`` as required (e.g. #5065's
    ``permission_approval_revoked``) needs it as real payload data.
    """
    from reyn.core.events.event_store import EventStore

    reyn_root = Path(reyn_root)
    if not reyn_root.is_dir():
        logger.warning(
            "emit_direct_event: %s does not exist; skipping P6 audit emit "
            "for event %r",
            reyn_root,
            kind,
        )
        return

    event_dir = reyn_root / "events" / "direct" / surface
    today = date.today().isoformat()  # YYYY-MM-DD
    # Use a date-named suffix so each day's events land in one predictable file.
    # max_bytes=0 / max_age_seconds=0 disables rotation — the suffix IS the date.
    store = EventStore(event_dir, max_bytes=0, max_age_seconds=0, suffix=f"_{today}")
    # #4966 (architect ruling, found via CI): this EventLog has no owner
    # who will ever call `drain()`/`stop_dispatch()` on it — the function
    # returns right after the one `emit()` call, nothing holds a
    # reference afterward. `_force_inline=True` declares that explicitly
    # rather than letting the mechanism GUESS it from "no loop is running"
    # (a guess that fails silently the moment a loop DOES happen to be
    # running around this call site, e.g. this function invoked
    # synchronously from inside an async caller). This is the ONE
    # sanctioned call site for `_force_inline` — see `EventLog.__init__`'s
    # own docstring for why it stays private and single-site, and the
    # bounding test that enforces exactly that (every direct-event caller
    # below routes through THIS one construction, not a construction of
    # its own).
    # Without it: a spawned-but-never-drained consumer task gets silently
    # abandoned, and when the loop eventually tears down, asyncio reports
    # it as its OWN "Task was destroyed but it is pending!" unhandled-
    # exception context — a SECOND, spurious `asyncio_unhandled_exception`
    # durably captured alongside whatever real exception the caller's own
    # diagnostics were trying to witness (found via
    # test_durable_capture_survives_prompt_toolkit_prompt_wait's own
    # `[event] = events` unpack going from 1 to 2 elements).
    event_log = EventLog(
        subscribers=[store], emitter=surface, track_audit_seq=track_audit_seq,
        _force_inline=True,
    )
    # Stamp `surface` into the payload itself too (not just the EventLog's
    # own `emitter` label, which the census/schema tooling does not treat
    # as a per-kind field) -- deterministic, not caller-wins, so a stray
    # caller-supplied `surface` key can never silently diverge from the
    # directory this event actually landed under.
    event_log.emit(kind, **{**payload, "surface": surface})


def emit_cli_event(kind: str, **payload) -> None:
    """Emit a one-off P6 event from a CLI context (no active session).

    Thin wrapper over :func:`emit_direct_event`: ``surface="cli"``,
    ``reyn_root`` located by walking up from ``Path.cwd()`` (a CLI process's
    cwd IS a reasonable project-root anchor, unlike a long-lived server's —
    see that function's own docstring), ``track_audit_seq=False`` (#4496
    PR-1: a one-off CLI event has no continuity to protect — a single event
    from a single process is not a series a gap can be detected in, so
    audit_seq is omitted entirely, architect's ruling, rather than always
    stamping a meaningless ``1``). If no ``.reyn/`` directory is found,
    logs a warning and returns silently (see :func:`emit_direct_event`).
    """
    reyn_dir = _find_reyn_dir(Path.cwd())
    if reyn_dir is None:
        logger.warning(
            "emit_cli_event: no .reyn/ directory found from %s; "
            "skipping P6 audit emit for event %r",
            Path.cwd(),
            kind,
        )
        return
    emit_direct_event(
        kind, surface="cli", reyn_root=reyn_dir, track_audit_seq=False, **payload,
    )
