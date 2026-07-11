"""race_cancellable — the shared cancel_event race for bounded external calls (#2813).

Any op that awaits a bounded external operation (subprocess, network call, MCP
transport) should race it against the per-turn ``cancel_event`` (set by
``Session.cancel_inflight()`` on Ctrl-C, threaded onto ``OpContext`` per #1470)
so a cancel takes effect IMMEDIATELY instead of waiting out the operation's own
internal timeout.

Precedent: ``sandboxed_exec``'s backends (seatbelt/landlock/noop) already race
their subprocess against ``cancel_event`` inline, per-backend. This module
extracts the same race into ONE reusable primitive so a new bounded-call site
(MCP, web_fetch, ...) doesn't reinvent it ad-hoc, and a future audit can grep
for this one seam instead of N per-backend copies.

Mechanism (same-task cancel scope, mirroring CPython's ``asyncio.timeout``): the
awaited coroutine runs INLINE in the caller's own task — a watcher task cancels
that HOST task when ``cancel_event`` fires, and the boundary here catches the
resulting ``CancelledError``, ``uncancel()``s it, and translates it to
:class:`Cancelled`. Running the coro in the host task (rather than a fresh task)
is load-bearing: it preserves task-affinity for any resource whose open/await/
close must stay on one task (e.g. the MCP SDK's ``stdio_client``/``ClientSession``
anyio cancel scopes, #2421), and it means a GENUINE external cancel of the host
task naturally propagates into the coro (no orphaned sub-task left running to its
own timeout — the bug a spawn-a-new-task design would have introduced).
"""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, TypeVar

if TYPE_CHECKING:
    from collections.abc import Awaitable

T = TypeVar("T")


class Cancelled(Exception):
    """Raised by :func:`race_cancellable` when ``cancel_event`` wins the race.

    The awaited coroutine's own ``finally``/``__aexit__`` teardown has already run
    (the ``CancelledError`` unwound through it in the host task) by the time this
    is raised, EXCEPT if that teardown itself raised — in which case that
    exception propagates instead and this is never raised. The caller only needs
    to translate this into its own cancelled-outcome result/event."""


async def race_cancellable(
    coro: "Awaitable[T]",
    *,
    cancel_event: "asyncio.Event | None",
) -> T:
    """Await ``coro``, but cancel it immediately if ``cancel_event`` fires first.

    ``cancel_event=None`` (no cancel-awareness available — OS-internal ops,
    non-interactive callers, pre-#1470 call sites) makes this a plain
    ``await coro`` — behavior-preserving default for every caller that doesn't
    pass one. Likewise falls back to a plain ``await`` if there is no running
    task to attach the cancel scope to (should not happen for op handlers).

    Unlike waiting for a fixed internal timeout (``asyncio.timeout`` /
    ``httpx`` request timeout / etc.), cancelling the host task interrupts
    ``coro`` at its NEXT await point (a network read, a subprocess wait, an MCP
    transport read) — genuinely immediate, not bounded by whatever internal
    deadline the operation itself was configured with.

    Raises :class:`Cancelled` if ``cancel_event`` won the race; re-raises a
    genuine EXTERNAL cancel of the host task as ``CancelledError`` (never
    swallows it — this is how ``Session.shutdown``'s hard-cancel still works);
    otherwise returns ``coro``'s result, or re-raises whatever ``coro`` raised.
    """
    if cancel_event is None:
        return await coro
    host_task = asyncio.current_task()
    if host_task is None:
        return await coro

    # Baseline cancellation count — an external cancel already in flight when we
    # enter must NOT be misread as ours (mirrors asyncio.timeout's _cancelling).
    baseline_cancelling = host_task.cancelling()
    fired = False

    async def _watcher() -> None:
        nonlocal fired
        try:
            await cancel_event.wait()
        except asyncio.CancelledError:
            return  # we were cancelled (coro finished first) — nothing to do
        fired = True
        host_task.cancel()

    watcher: "asyncio.Task[None]" = asyncio.ensure_future(_watcher())
    already_uncancelled = False
    try:
        return await coro
    except asyncio.CancelledError:
        # Our watcher's cancel() is the ONLY cancel we ever issue (exactly once,
        # guarded by ``fired``). uncancel() removes exactly it; the RETURNED count
        # is whatever external cancels remain. <= baseline → only ours was pending
        # → this is the event cancel, translate it. > baseline → a genuine external
        # cancel is ALSO pending → honor it (re-raise), our extra cancel already
        # removed so nothing leaks upward.
        if fired:
            already_uncancelled = True
            if host_task.uncancel() <= baseline_cancelling:
                raise Cancelled("cancel_event fired before the operation completed") from None
        raise
    finally:
        if not watcher.done():
            watcher.cancel()
            try:
                await watcher
            except asyncio.CancelledError:
                pass
        elif fired and not already_uncancelled:
            # The watcher fired but coro did NOT raise CancelledError (it returned
            # normally, or raised a different exception, before our cancel() was
            # delivered at an await point). A stray pending cancel would otherwise
            # leak into the CALLER at its next await — consume it here. If an
            # external cancel is also pending underneath ours, re-arm it so it is
            # not silently dropped (we only meant to remove our own).
            if host_task.uncancel() > baseline_cancelling:
                host_task.cancel()
