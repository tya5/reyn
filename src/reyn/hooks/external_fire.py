"""reyn.hooks.external_fire — bounded, non-blocking dispatch helper for
OUT-OF-SESSION external-event ingress (#2608 H5: cron + webhook; bounded by
#2620).

H1 (``mcp_resource_updated``) and H4 (``file_changed``) both fire their hook
from INSIDE the session's own process — a bounded ``asyncio.Queue`` drained
by ONE dedicated background task, decoupling the producer (MCP receive-loop
task / watchdog thread) from the hook dispatch (see
``reyn.hooks.ingress._BoundedEventBridge``: fixed-size queue, ``put_nowait``,
drop-newest-and-log on overflow). Cron and webhook ingress have no such
producer/drain split: ``reyn.runtime.cron.routing.resolve_cron_session`` /
``reyn.runtime.webhook_routing.resolve_webhook_session`` resolve the target
Session directly at fire/request time, in the SAME coroutine that also does
the ingress's own delivery work (the cron job's inbox push / the webhook
plugin's HTTP response) — there is no already-running producer task/thread
to own a bridge the way H1/H4's adapters do.

:func:`fire_and_forget` closes that gap (#2620): rather than scheduling an
unbounded ``asyncio.create_task`` per fire (the pre-#2620 shape — a webhook
flood could spawn arbitrarily many concurrent hook-dispatch tasks, each
running the configured hook actions), each SESSION now gets its own
:class:`_SessionFireBridge` — the exact ``_BoundedEventBridge`` shape
(fixed-size ``asyncio.Queue`` + one lazily-started sequential drain task),
lazily created on first fire and reused across every subsequent fire for
that session. A burst of fires beyond the queue's ``maxsize`` drops the
NEWEST event and logs a warning rather than growing the queue unboundedly or
spawning another concurrent dispatch task — cron_fired is rate-limited by
the schedule in practice (this bound is inert for it), webhook_received is
the surface this actually protects (an inbound webhook flood, #2608 H5's
"semi-trusted surface" note).

``session.dispatch_external_event`` already isolates per-hook failures
internally (never raises — see ``reyn.hooks.dispatcher``); the drain loop's
own ``try/except`` exists purely so one bad dispatch never kills the whole
session's drain task (mirrors ``_BoundedEventBridge._drain``'s per-event
isolation).
"""
from __future__ import annotations

import asyncio
import logging
import weakref
from typing import Any

logger = logging.getLogger(__name__)

# Mirrors reyn.hooks.ingress._BoundedEventBridge's own default (the H1/H4
# in-process bridge's ``maxsize=32``) so H5's out-of-process bound matches
# the bound its in-process siblings already use for the same class of risk.
_DEFAULT_MAXSIZE = 32


class _SessionFireBridge:
    """Per-Session bounded dispatch bridge (#2620) — the ``_BoundedEventBridge``
    shape (``reyn.hooks.ingress``) applied to the out-of-process H5 path: a
    bounded ``asyncio.Queue`` plus a lazily-created background drain task
    that awaits ``session.dispatch_external_event`` for each queued
    ``(point, template_vars)`` pair, ONE AT A TIME (never concurrently for
    the same session), with per-event ``try/except`` (one bad dispatch must
    never kill the drain task, same discipline as
    ``_BoundedEventBridge._drain``).

    One instance per Session, created lazily on first :func:`fire_and_forget`
    call for that session and cached in the module-level
    ``_session_bridges`` WeakKeyDictionary so a session that never fires an
    external-event hook never allocates a queue/task at all (byte-identical
    to a build with no H5 dispatch)."""

    def __init__(self, *, session: Any, maxsize: int) -> None:
        self._session = session
        self._maxsize = maxsize
        self._queue: "asyncio.Queue[tuple[str, dict]] | None" = None
        self._drain_task: "asyncio.Task | None" = None
        # #2620 Observability lens: a fail-visible counter (public via
        # ``dropped_dispatch_count``) alongside the WARNING log line — a test
        # or operator can confirm the bound actually triggered without
        # scraping logs or reaching into this bridge's private state.
        self._drop_count = 0

    def submit(self, point: str, template_vars: dict) -> None:
        """SYNCHRONOUS, non-blocking, never raises. Lazily starts this
        session's drain task on first call. A full queue drops the NEWEST
        ``(point, template_vars)`` pair and logs — bounded by construction,
        the producer (the webhook/cron ingress coroutine) is never blocked
        and never spawns an additional concurrent dispatch task."""
        if self._queue is None:
            self._queue = asyncio.Queue(maxsize=self._maxsize)
        if self._drain_task is None or self._drain_task.done():
            self._drain_task = asyncio.create_task(self._drain())
        try:
            self._queue.put_nowait((point, template_vars))
        except asyncio.QueueFull:
            self._drop_count += 1
            logger.warning(
                "external-event hook dispatch queue full (maxsize=%d) — dropping "
                "point=%r (fires arriving faster than hooks can be dispatched "
                "for this session)",
                self._maxsize, point,
            )

    def pending_count(self) -> int:
        """Public, snapshot-style read of the number of ``(point,
        template_vars)`` pairs still queued (not yet drained) for this
        session. ``0`` before the first :meth:`submit` call (no queue
        allocated yet)."""
        return 0 if self._queue is None else self._queue.qsize()

    def dropped_count(self) -> int:
        """Public, snapshot-style read of the cumulative number of pairs
        dropped by this bridge because the queue was full at ``submit``
        time (#2620)."""
        return self._drop_count

    async def _drain(self) -> None:
        assert self._queue is not None
        while True:
            point, template_vars = await self._queue.get()
            try:
                await self._session.dispatch_external_event(point, template_vars)
            except Exception:  # noqa: BLE001 — one bad dispatch must not kill the drain task
                logger.warning(
                    "external-event hook dispatch failed for point=%r", point, exc_info=True,
                )


# Module-level, keyed by session identity (weak so a garbage-collected
# session's bridge/queue is reclaimed with it rather than pinned forever —
# #2620 introduces a per-session bridge that outlives any single fire, unlike
# the pre-#2620 one-task-per-fire shape, so this cache must not be a plain
# dict).
_session_bridges: "weakref.WeakKeyDictionary[Any, _SessionFireBridge]" = (
    weakref.WeakKeyDictionary()
)


def fire_and_forget(
    session: Any, point: str, template_vars: dict, *, maxsize: int = _DEFAULT_MAXSIZE,
) -> None:
    """Schedule ``session.dispatch_external_event(point, template_vars)``
    through ``session``'s bounded dispatch bridge (#2620) rather than
    awaiting it inline or spawning an unbounded background task per call.

    Never raises into the caller — safe to call unconditionally from an
    ingress's fast path, empty-hook-registry included (``dispatch`` itself is
    a no-op when nothing is registered for ``point``, so an empty registry is
    byte-identical to a build with no hook mechanism at all beyond the
    negligible cost of one queued no-op dispatch). ``maxsize`` mirrors
    ``_BoundedEventBridge``'s own constructor knob — overridable per call
    for a caller that wants a different bound than the default, but every
    current call site (``reyn.hooks.ingress``'s Cron/Webhook adapters) uses
    the default.
    """
    bridge = _session_bridges.get(session)
    if bridge is None:
        bridge = _SessionFireBridge(session=session, maxsize=maxsize)
        _session_bridges[session] = bridge
    bridge.submit(point, template_vars)


def pending_dispatch_count(session: Any) -> int:
    """Public, snapshot-style read (Observability lens, #2620): the number of
    ``fire_and_forget`` calls for ``session`` still queued and not yet
    drained. ``0`` if ``session`` has never called :func:`fire_and_forget`
    (no bridge allocated yet) — the no-fire-yet equivalence property."""
    bridge = _session_bridges.get(session)
    return 0 if bridge is None else bridge.pending_count()


def dropped_dispatch_count(session: Any) -> int:
    """Public, snapshot-style read (Observability lens, #2620): the
    cumulative number of ``fire_and_forget`` calls for ``session`` dropped
    because its dispatch queue was full at submit time. ``0`` if ``session``
    has never called :func:`fire_and_forget`."""
    bridge = _session_bridges.get(session)
    return 0 if bridge is None else bridge.dropped_count()


__all__ = ["dropped_dispatch_count", "fire_and_forget", "pending_dispatch_count"]
