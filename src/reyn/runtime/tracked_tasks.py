"""#4759: a single funnel for every fire-and-forget background task a
``Session`` (or a sub-component it owns — ``SpawnTracker``, ``ChainManager``,
``OutboxHub``, the hooks bridge, ...) spawns via ``asyncio.create_task``.

Root cause this closes: before this module existed, each such task lived on
its own ad hoc field (``SpawnTracker._vanish_task``, ``OutboxHub._drain_task``,
a bare local in ``session_api.py``, ...), and ``AgentRegistry.shutdown()`` (or
whatever else needed to know "is anything still cleaning up") had to
ENUMERATE those fields by name to find them. ``SpawnTracker._vanish_task`` —
a task that itself closes this session's held MCP connections — was never
added to that enumeration, so a normal ``registry.shutdown()`` (and, in
production, an ordinary ``/quit``) could return while it was still mid-flight
or not yet even scheduled, orphaning the OS subprocess it was about to close
(#4759). Adding ``_vanish_task`` to the enumeration by name would only move
the same defect up one level: the NEXT background task some future PR adds
would again need someone to remember to list it.

The fix is a single owned collection instead of a named list: every producer
calls :meth:`TrackedTaskSet.spawn` (never ``asyncio.create_task`` directly)
and :meth:`TrackedTaskSet.aclose` is the ONE thing a teardown caller needs to
know about, regardless of how many task-owning sub-components exist now or
are added later — a 9th call site that goes through ``spawn`` is covered by
construction, not by a reviewer remembering to touch a teardown method.

**Two independent axes, not one.** ``disposition`` answers HOW a task folds
(await it to completion vs. cancel-then-join it) — it says nothing about
WHEN it is safe to fold. ``appends_wal`` answers that second question, and —
this took two review rounds to name correctly (lead-coder + architect
co-vet, #4759) — it must name the ACTUAL invariant ``Session.await_quiescent``
declares in its own docstring, not a proxy for it:

    "no WAL append can still land" / Coverage: "the exhaustive set of
    APPEND-CAPABLE spawned tasks" (#1533's own table)

The FIRST version of this axis was named ``scope`` (``"quiesce"`` vs.
``"session"``, i.e. "does this task's LIFETIME track a turn/rewind or the
whole session") — plausible-sounding, and it shipped a real regression
anyway: an unscoped ``aclose()`` call from ``await_quiescent`` (which runs
during a REWIND, not only shutdown) cancelled OutboxHub's drain loop, and
the session silently stopped answering (caught by CI:
``tests/runtime/test_slash_rewind_self_cancel_3362.py``). The FIX for that
regression was scoping by lifetime — but the axis's NAME was still wrong:
"session-scoped" and "does not append to the WAL" happen to coincide for
every producer that exists TODAY, but they are not the same property, and a
future task that is BOTH session-lifetime AND WAL-appending would silently
escape ``await_quiescent``'s protection under a lifetime-named axis — the
same invisible-failure shape #4759 itself is about, one level up (architect
co-vet, #4765). ``appends_wal`` names the actual thing being asked:

- ``True`` — this task's own execution can result in a durable WAL append
  landing (chain-timeout watchdogs firing ``chain_timeout_fired``;
  fire-and-forget intervention-dispatch/answer-consumed appends; the
  ephemeral-vanish task's own ``remove_session`` → ``session_vanished``
  append). ``await_quiescent`` MUST drain these before a rewind's
  reset-record, or a straggler append lands past it (the #1533/#2115 bug
  class) — cancel_join tasks here are drop-safe AND reversible (restore()
  re-arms them from the recovered snapshot); the vanish task (disposition
  "await") is instead genuinely awaited to completion.
- ``False`` (the default) — this task's own execution never itself appends
  to the WAL (OutboxHub's drain loop, the hook-bus bridge,
  hooks/external_fire's drain loop, a cross-session cancel-inflight
  forward, restored-intervention watchers whose own cancel is reset_for_
  rewind's separate, pre-existing mechanism, a maintenance truncation
  check) — ``await_quiescent`` must NOT touch these; only real session
  teardown may.

``await_quiescent`` calls ``aclose(appends_wal=True)`` (drains only the
tasks that could contaminate the reset-record); ``Session.
aclose_background_tasks`` (real shutdown, via ``AgentRegistry.shutdown()``)
calls plain ``aclose()`` (every producer, both values). Declared BY THE
SPAWNER at ``spawn()``/``register()`` time — the same reasoning that put
``disposition`` there: a 9th producer states its own two properties where
it is created, so nothing downstream needs to enumerate producers to get
either axis right.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from typing import Any, Literal

logger = logging.getLogger(__name__)

#: "await" — the task performs real work that must be allowed to finish (the
#: ephemeral-vanish teardown task, which itself closes this session's held
#: MCP connections — cancelling it would defeat its own purpose). "cancel_join"
#: — the task is drop-safe fire-and-forget work (a WAL append, a chain-timeout
#: watchdog, a forwarder loop) — cancelling is correct, and for the WAL/chain
#: cases explicitly reversible (reconstruction re-arms them from the WAL), so
#: :meth:`TrackedTaskSet.aclose` cancels these before joining them.
Disposition = Literal["await", "cancel_join"]

#: Cap for :meth:`TrackedTaskSet.aclose`'s re-drain loop — the SAME shape as
#: the pre-#4759 ``await_quiescent``-only ``_QUIESCE_MAX_ROUNDS`` this
#: replaces (#2115: a joined task can itself spawn a NEW tracked task — e.g.
#: a re-armed chain timer, or the vanish task's own ``remove_session()``
#: touching another tracked field — that a single snapshot-and-gather pass
#: would miss), carrying over its ACTUAL value (50, not re-derived here) —
#: #2115's own reasoning ("finite + cancel-requested + spawns no new
#: user-work under a rewind, converges in 1-2 rounds; the cap is purely a
#: guard against a pathological spin, logged, never silently looped") was
#: scoped to WAL-append tasks specifically. This funnel now also carries
#: "await"-disposition tasks (the vanish task) that perform real work and
#: are not cancel-requested, so convergence is not guaranteed in 1-2 rounds
#: the same way — reusing the wider, already-established 50 rather than
#: inventing a smaller number for a case #2115 never measured.
_MAX_ACLOSE_ROUNDS = 50


class TrackedTaskSet:
    """Owns every background task spawned through it, with a per-task
    disposition + ``appends_wal`` flag recorded at spawn time. See module
    docstring for why this exists instead of N separately-named task
    fields, and for the two-axis (disposition, appends_wal) design."""

    def __init__(self) -> None:
        self._tasks: "dict[asyncio.Task, tuple[Disposition, bool]]" = {}

    def __len__(self) -> int:
        """Total tracked task count (done or not) — lets a generic
        container-measurement surface (``resident_stats.py``'s #4497
        report) treat this like any other sized container without needing
        to special-case it."""
        return len(self._tasks)

    def __iter__(self):
        """Iterate the tracked ``asyncio.Task`` objects — same rationale
        as ``__len__``; ``pending()`` remains the intentional-subset (not
        yet done) read for callers that specifically want that."""
        return iter(self._tasks)

    def spawn(
        self,
        coro: "Coroutine[Any, Any, Any]",
        *,
        disposition: Disposition = "cancel_join",
        appends_wal: bool = False,
        name: str,
    ) -> asyncio.Task:
        """Create and track a background task. Use this instead of a bare
        ``asyncio.create_task`` for anything that outlives the call that
        creates it — that is the entire point of this module. ``name`` is
        REQUIRED (not optional with a generic default): a diagnostic that
        can only say "Task-7" — asyncio's own default when no name is
        given — is a diagnostic that doesn't diagnose (#4765 co-vet: this
        is what made the reentrancy-exclusion warning and the
        fixpoint-exhaustion warning unreadable for ``register()``-tracked
        tasks before this was enforced here too). ``appends_wal`` defaults
        to ``False`` (the safer default — see module docstring): pass
        ``appends_wal=True`` explicitly only for a task whose own execution
        can result in a durable WAL append landing."""
        task = asyncio.create_task(coro, name=name)
        self._tasks[task] = (disposition, appends_wal)
        task.add_done_callback(lambda t: self._tasks.pop(t, None))
        return task

    def register(
        self, task: asyncio.Task, *,
        disposition: Disposition = "cancel_join", appends_wal: bool = False,
    ) -> asyncio.Task:
        """Track an ALREADY-CREATED task (e.g. one made via
        ``asyncio.ensure_future``/``loop.create_task`` at a call site that
        needs the task object before it can hand it off — the caller MUST
        have named it via ``asyncio.create_task(..., name=...)`` /
        ``asyncio.ensure_future`` supports no ``name=`` kwarg itself, so
        name it via ``task.set_name(...)`` before registering if needed).
        Prefer :meth:`spawn` when the caller controls creation; this exists
        for the sites that don't. Same ``appends_wal`` default/guidance as
        :meth:`spawn`."""
        self._tasks[task] = (disposition, appends_wal)
        task.add_done_callback(lambda t: self._tasks.pop(t, None))
        return task

    def pending(self) -> "list[asyncio.Task]":
        """Read-only introspection: currently-tracked, not-yet-done tasks."""
        return [t for t in self._tasks if not t.done()]

    async def aclose(self, *, appends_wal: "bool | None" = None, caller: str = "") -> None:
        """Drain tracked tasks to a fixpoint: cancel every ``"cancel_join"``
        task, leave every ``"await"`` task to run to completion on its own,
        then join everything currently tracked — looping because a joined
        task may itself register a new one (the same re-entrancy #2115
        already fixed for WAL-append tasks, now generalised to every
        producer through this one seam). Best-effort: a task's own
        exception is swallowed (logged) so one faulty task can't abort
        draining the rest. NOT independently time-bounded — callers that
        must not block ``/quit`` indefinitely (i.e.
        ``AgentRegistry.shutdown()``) wrap this in their own bounded
        ``asyncio.wait_for``; this method has no opinion on that budget.

        ``appends_wal`` filters WHICH tracked tasks this call touches (see
        module docstring's "Two independent axes" section) — ``None`` (the
        default) drains every task regardless of the flag, the real-shutdown
        case (``Session.aclose_background_tasks``, called from
        ``AgentRegistry.shutdown()``). Pass ``appends_wal=True`` for a
        mid-life quiesce point (``Session.await_quiescent``, called during
        a REWIND, NOT a shutdown) — this touches ONLY tasks whose own
        execution can land a durable WAL append, leaving every
        ``appends_wal=False`` task (OutboxHub's drain loop, the hook-bus
        bridge, ...) untouched and running, because the session is
        expected to keep serving turns after the quiesce point returns.
        #4759/#4765 review: an EARLIER version of this axis was named by
        task LIFETIME ("session-scoped" vs "quiesce-scoped") rather than by
        this actual invariant — the two happened to coincide for every
        producer that existed then, but a future session-lifetime task that
        ALSO appends to the WAL would have silently escaped protection
        under that naming. Naming the axis after the real invariant
        ``await_quiescent`` declares (see its own docstring: "no WAL append
        can still land") closes that gap by construction instead of by
        convention.

        ``caller`` is a short, free-text label for WHO is calling this —
        purely diagnostic (folded into the reentrancy-exclusion warning
        below), because the excluded task's own name does not say who
        called ``aclose()`` (#4765 co-vet: the reentrancy warning could
        previously only report which task was skipped, not which caller
        hit the reentrant case — insufficient to tell an expected
        exclusion (the vanish task's own internal call) apart from an
        unexpected one (a hypothetical future ``AgentRegistry.shutdown()``
        call landing reentrant, which would mean its "always non-reentrant"
        expectation had broken). Pass e.g. ``caller="await_quiescent"`` or
        ``caller="AgentRegistry.shutdown"`` at each call site.

        #4759 re-entrancy: a tracked "await"-disposition task can itself end
        up calling ``aclose()`` (the ephemeral-vanish task runs
        ``remove_session()``, which awaits ``Session.await_quiescent()``,
        which calls ``self._background_tasks.aclose(appends_wal=True,
        caller="await_quiescent")``) — while that call is on the stack, the
        vanish task is STILL tracked (its done-callback hasn't fired; it
        hasn't returned yet). A naive ``asyncio.gather`` over every pending
        task would then include ``asyncio.current_task()`` alongside itself
        — measured directly (strip the exclusion below and run
        ``tests/runtime/test_4759_tracked_task_set.py``): this does not
        raise, it HANGS (``current_task`` wait on a gather that is itself
        waiting on ``current_task`` to finish, which it cannot do until the
        gather it's awaiting returns — the exact class of silent stall
        #4759 started from, now reproduced one layer up, in the fix's own
        primitive, if the exclusion is removed). So a reentrant call
        excludes ``current_task()`` from its OWN drain.

        **The guarantee this weakens, precisely**: a call to ``aclose()``
        made FROM a task this same ``TrackedTaskSet`` is currently tracking
        (i.e. ``asyncio.current_task()`` is itself a tracked, not-yet-done
        task) returns once every OTHER matching-filter tracked task has
        settled — it does NOT wait for its own caller-task, and a caller in
        that position cannot read "aclose() returned" as "everything is
        done" (its own task, by definition, is not done yet — it's still
        running the ``aclose()`` call). A call made from any OTHER task has
        no such gap: EVERY matching-filter tracked task, including one that
        is mid-reentrant-``aclose()`` right now, is included in that call's
        own drain and genuinely awaited to completion.

        ``AgentRegistry.shutdown()``'s OWN call is EXPECTED to always be
        non-reentrant (the registry's task is never itself one this tracker
        owns), which is what makes it correct despite the vanish task's own
        internal reentrant call resolving early. This expectation is NOT
        exhaustively verified across every ``shutdown()`` call site in the
        codebase (6 call sites measured at #4759 review time; the 3
        top-level CLI entry points are self-evidently not a tracked task,
        but the other 3 — including a slash/dispatch.py path reachable via
        hook/intervention machinery — were not individually traced for
        whether a tracked task could ever be the one calling `shutdown()`).
        Rather than assert an unverified absolute, a reentrant exclusion
        below always logs a WARNING naming the excluded task, its
        disposition, and the ``caller`` label passed in — reentrancy itself
        is a NORMAL, expected event for the vanish task's own internal
        call, so this is diagnostic, not an error; but if this warning is
        ever observed with ``caller="AgentRegistry.shutdown"`` specifically,
        that call's non-reentrancy assumption has broken and its
        "everything is done" reading is no longer trustworthy.
        """
        current = asyncio.current_task()
        if current is not None and current in self._tasks and not current.done():
            # Logged ONCE per aclose() call, not per fixpoint round below —
            # `current` cannot become done() while it is the task running
            # THIS code, so re-checking inside the loop would just repeat
            # the same warning up to _MAX_ACLOSE_ROUNDS times.
            disp, wal = self._tasks[current]
            logger.warning(
                "TrackedTaskSet.aclose(caller=%r): called reentrantly from "
                "a task (%r, disposition=%r, appends_wal=%r) this tracker "
                "is itself still tracking -- that task is excluded from "
                "THIS call's own drain (see aclose()'s own docstring for "
                "why). Normal for the ephemeral-vanish task's internal "
                "call; if caller=='AgentRegistry.shutdown' here, its "
                "non-reentrancy assumption has broken.",
                caller, current.get_name(), disp, wal,
            )
        for _ in range(_MAX_ACLOSE_ROUNDS):
            pending = [
                t for t, (_disp, t_wal) in self._tasks.items()
                if not t.done() and t is not current and (appends_wal is None or t_wal == appends_wal)
            ]
            if not pending:
                return
            # #4986 variant B: name what's about to be waited on, BEFORE the
            # wait below — the one thing a hang leaves no other trace of.
            # `pytest-timeout`'s dump is faulthandler-based (OS-thread
            # tracebacks); an asyncio Task is a coroutine on the ONE event-
            # loop thread, not a thread of its own, so that dump can only
            # ever say "the loop is polling" — never which task, which is
            # why this line exists (see this method's own module for the
            # #4986 design ruling; verified directly before this fix landed:
            # faulthandler.dump_traceback() over a real hung task names only
            # the loop's own frame, never the task). Logged once per
            # fixpoint round, right here (not once per aclose() call): a
            # task that finishes mid-round drops off this list on the NEXT
            # round, so a normal (non-hanging) drain logs this at most once
            # and then never again once `pending` empties above — the noise
            # guard witness #4986's own acceptance table requires.
            # `warning` is a requirement, not a default: lowering it to
            # info/debug drops this out of pytest's own failure report,
            # silently deleting the one trace a real hang leaves — do not
            # lower it even though a normal shutdown also logs it once
            # (a resident cancel_join task is routinely still pending).
            logger.warning(
                "TrackedTaskSet.aclose(caller=%r): waiting on %d tracked "
                "task(s): %s",
                caller, len(pending),
                ", ".join(
                    f"{t.get_name()!r} disposition={self._tasks[t][0]!r} "
                    f"appends_wal={self._tasks[t][1]!r}"
                    for t in pending
                ),
            )
            for task in pending:
                disp, _wal = self._tasks[task]
                if disp == "cancel_join":
                    task.cancel()
            results = await asyncio.gather(*pending, return_exceptions=True)
            for task, result in zip(pending, results, strict=True):
                if isinstance(result, BaseException) and not isinstance(
                    result, asyncio.CancelledError,
                ):
                    logger.warning(
                        "TrackedTaskSet.aclose: tracked task %r raised during "
                        "teardown: %r", task.get_name(), result,
                    )
        else:
            still_pending = [
                t for t, (_d, t_wal) in self._tasks.items()
                if not t.done() and (appends_wal is None or t_wal == appends_wal)
            ]
            logger.warning(
                "TrackedTaskSet.aclose(caller=%r): did not drain to a "
                "fixpoint in %d rounds -- %d task(s) still tracked: %r",
                caller, _MAX_ACLOSE_ROUNDS, len(still_pending),
                [t.get_name() for t in still_pending],
            )
