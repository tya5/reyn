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

#: Mirrors ``session.py``'s own ``_QUIESCE_MAX_ROUNDS`` fixpoint bound: a
#: joined task can itself spawn a NEW tracked task (e.g. a re-armed chain
#: timer, or a vanish task's own ``remove_session()`` touching another
#: tracked field) that a single snapshot-and-gather pass would miss — loop to
#: a fixpoint instead of assuming one pass drains everything.
_MAX_ACLOSE_ROUNDS = 8


class TrackedTaskSet:
    """Owns every background task spawned through it, with a per-task
    disposition recorded at spawn time. See module docstring for why this
    exists instead of N separately-named task fields."""

    def __init__(self) -> None:
        self._tasks: "dict[asyncio.Task, Disposition]" = {}

    def spawn(
        self,
        coro: "Coroutine[Any, Any, Any]",
        *,
        disposition: Disposition = "cancel_join",
        name: "str | None" = None,
    ) -> asyncio.Task:
        """Create and track a background task. Use this instead of a bare
        ``asyncio.create_task`` for anything that outlives the call that
        creates it — that is the entire point of this module."""
        task = asyncio.create_task(coro, name=name)
        self._tasks[task] = disposition
        task.add_done_callback(lambda t: self._tasks.pop(t, None))
        return task

    def register(self, task: asyncio.Task, *, disposition: Disposition = "cancel_join") -> asyncio.Task:
        """Track an ALREADY-CREATED task (e.g. one made via
        ``asyncio.ensure_future``/``loop.create_task`` at a call site that
        needs the task object before it can hand it off). Prefer
        :meth:`spawn` when the caller controls creation; this exists for the
        sites that don't."""
        self._tasks[task] = disposition
        task.add_done_callback(lambda t: self._tasks.pop(t, None))
        return task

    def pending(self) -> "list[asyncio.Task]":
        """Read-only introspection: currently-tracked, not-yet-done tasks."""
        return [t for t in self._tasks if not t.done()]

    async def aclose(self) -> None:
        """Drain every tracked task to a fixpoint: cancel every
        ``"cancel_join"`` task, leave every ``"await"`` task to run to
        completion on its own, then join everything currently tracked —
        looping because a joined task may itself register a new one (the
        same re-entrancy #2115 already fixed for WAL-append tasks, now
        generalised to every producer through this one seam). Best-effort:
        a task's own exception is swallowed (logged) so one faulty task
        can't abort draining the rest. NOT independently time-bounded —
        callers that must not block ``/quit`` indefinitely (i.e.
        ``AgentRegistry.shutdown()``) wrap this in their own bounded
        ``asyncio.wait_for``; this method has no opinion on that budget.

        #4759 re-entrancy: a tracked "await"-disposition task can itself end
        up calling ``aclose()`` (the ephemeral-vanish task runs
        ``remove_session()``, which awaits ``Session.await_quiescent()``,
        which now calls ``self._background_tasks.aclose()``) — while that
        call is on the stack, the vanish task is STILL tracked (its
        done-callback hasn't fired; it hasn't returned yet). A naive
        ``asyncio.gather`` over every pending task would then include
        ``asyncio.current_task()`` alongside itself — measured directly
        (strip the exclusion below and run
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
        task) returns once every OTHER tracked task has settled — it does
        NOT wait for its own caller-task, and a caller in that position
        cannot read "aclose() returned" as "everything is done" (its own
        task, by definition, is not done yet — it's still running the
        ``aclose()`` call). A call made from any OTHER task (the ordinary
        case — ``AgentRegistry.shutdown()``'s own call is always non-
        reentrant, since the registry's task is never itself one this
        tracker owns) has no such gap: EVERY tracked task, including one
        that is mid-reentrant-``aclose()`` right now, is included in that
        call's own drain and genuinely awaited to completion. This is what
        makes ``AgentRegistry.shutdown()``'s call correct despite the
        vanish task's own internal reentrant call resolving early — the
        outer, non-reentrant call still waits for the vanish task itself to
        finish, exactly as #4759 requires.
        """
        current = asyncio.current_task()
        for _ in range(_MAX_ACLOSE_ROUNDS):
            pending = [t for t in self._tasks if not t.done() and t is not current]
            if not pending:
                return
            for task in pending:
                if self._tasks.get(task) == "cancel_join":
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
            logger.warning(
                "TrackedTaskSet.aclose: did not drain to a fixpoint in %d "
                "rounds — %d task(s) still tracked",
                _MAX_ACLOSE_ROUNDS, len(self.pending()),
            )
