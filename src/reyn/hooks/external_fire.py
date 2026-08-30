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
import functools
import logging
import weakref
from typing import Any

logger = logging.getLogger(__name__)

# Mirrors reyn.hooks.ingress._BoundedEventBridge's own default (the H1/H4
# in-process bridge's ``maxsize=32``) so H5's out-of-process bound matches
# the bound its in-process siblings already use for the same class of risk.
_DEFAULT_MAXSIZE = 32

# #5515: fail-visible cadence for a queue-full drop in _SessionFireBridge.
# submit() (below) — same constant, same reasoning, as
# reyn.hooks.ingress._AUDIT_EVERY_N_DROPS (matched to src/reyn/hooks/bus.py:83's
# own ``_AUDIT_EVERY_N_DROPS``, #2886's first-drop/every-Nth discipline) —
# this bridge is the SECOND of the two sites #5515 closes the audit gap
# for; the PR that landed the first (``_BoundedEventBridge``) already
# established the cadence value, this one reuses it rather than re-deriving.
_AUDIT_EVERY_N_DROPS = 100

# #5515 review (lead-coder BLOCKING, 2026-08-30): the string this class
# identifies itself by at TWO independent call sites -- observe_drain_task_
# death's own ``label=`` (submit(), below) and ingress_bridge_dropped's own
# ``source=`` (_audit_drop, below). Before this constant the two were
# separately-typed literals that happened to read the same; nothing forced
# them to stay in sync (a rename of one, missed at the other, would desync
# silently -- neither call site would error, only an operator correlating
# the two by eye would notice). One constant, both sites read it.
_SOURCE_LABEL = "_SessionFireBridge"


class _SessionFireBridge:
    """Per-Session bounded dispatch bridge (#2620) — the ``_BoundedEventBridge``
    shape (``reyn.hooks.ingress``) applied to the out-of-process H5 path: a
    bounded ``asyncio.Queue`` plus a lazily-created background drain task
    that folds whatever is queued at drain time into ONE
    ``session.dispatch_external_event_batch`` call per POINT (#5516 — was
    one call per event; see ``reyn.hooks.fold.drain_folded``, the shared
    countdown/qsize()-before-dispatch primitive this bridge,
    ``ingress._BoundedEventBridge``, and ``composed_consumer.
    ComposedEventConsumer`` all use), with per-BATCH ``try/except`` (one
    bad dispatch must never kill the drain task, same discipline as
    ``_BoundedEventBridge._drain``).

    Unlike ``_BoundedEventBridge`` (one queue per ADAPTER, hence single-
    kind), this bridge is one queue per SESSION — ``cron_fired`` and
    ``webhook_received`` fires for the same session share it (both route
    through :func:`fire_and_forget`), so a raw drained batch can mix
    points. Grouped by point BEFORE dispatch (same shape
    ``ComposedEventConsumer`` uses for its own kind-mixing subscription
    queue) — folding one point's events must never silently include a
    different point's.

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
        # Cumulative (never reset) — distinct from ``_dropped_since_last_batch``
        # below (#5516), which read-and-resets per dispatched batch to feed
        # ``skipped_session_wide``.
        self._drop_count = 0
        self._dropped_since_last_batch = 0

    def submit(self, point: str, template_vars: dict) -> None:
        """SYNCHRONOUS, non-blocking, never raises. Lazily starts this
        session's drain task on first call. A full queue drops the NEWEST
        ``(point, template_vars)`` pair and logs — bounded by construction,
        the producer (the webhook/cron ingress coroutine) is never blocked
        and never spawns an additional concurrent dispatch task."""
        if self._queue is None:
            self._queue = asyncio.Queue(maxsize=self._maxsize)
        if self._drain_task is None or self._drain_task.done():
            # #4759: route through the owning session's task funnel
            # (tracked_tasks.py) when available, so AgentRegistry.shutdown()
            # can reach this drain loop without needing to know this bridge
            # exists — getattr-guarded since ``self._session`` is typed Any
            # (a test double may not carry the attribute).
            tracker = getattr(self._session, "_background_tasks", None)
            if tracker is not None:
                # appends_wal=False (the default, stated explicitly here):
                # this drain loop dispatches external-event hooks, it does
                # not itself append to the WAL -- a mid-rewind quiesce point
                # must never cancel it (#4759 review: an earlier version of
                # this axis was lifetime-named ("scope") rather than
                # invariant-named, and a rewind's quiesce killed OutboxHub's
                # own drain loop the same way; this bridge is the same
                # shape).
                self._drain_task = tracker.spawn(
                    self._drain(), disposition="cancel_join", appends_wal=False,
                    name="external-fire-drain",
                )
            else:
                # #4765 co-vet (architect): a session-like object without
                # `_background_tasks` (a test double, typically) falls back
                # to an untracked task here, same as before this funnel
                # existed. Warned (not silent) so a caller that SHOULD have
                # a real Session but doesn't has a signal to find.
                logger.warning(
                    "_SessionFireBridge: session has no _background_tasks -- "
                    "external-event drain task is NOT reachable from "
                    "AgentRegistry.shutdown()'s drain (see tracked_tasks.py)."
                )
                self._drain_task = asyncio.create_task(self._drain())
            # #5521 (architect ruling): observe — never swallow — this
            # drain task's own eventual death, for EITHER branch above.
            # ``self._session`` is typed Any (a test double may not carry
            # ``_audit_events``) — same getattr-guarded posture as
            # ``_background_tasks`` a few lines above, not a new idiom.
            # See ingress.py's identical wiring / observe_drain_task_death's
            # own docstring for the full contract.
            from reyn.hooks.fold import observe_drain_task_death
            audit_events = getattr(self._session, "_audit_events", None)
            assert self._drain_task is not None  # just assigned by either branch above
            self._drain_task.add_done_callback(
                functools.partial(
                    observe_drain_task_death,
                    emit_event=(audit_events.emit if audit_events is not None else None),
                    label=_SOURCE_LABEL,
                )
            )
        try:
            self._queue.put_nowait((point, template_vars))
        except asyncio.QueueFull:
            self._drop_count += 1
            self._dropped_since_last_batch += 1
            logger.warning(
                "external-event hook dispatch queue full (maxsize=%d) — dropping "
                "point=%r (fires arriving faster than hooks can be dispatched "
                "for this session)",
                self._maxsize, point,
            )
            self._audit_drop(point)

    def _audit_drop(self, point: str) -> None:
        """Fire a metadata-only ``ingress_bridge_dropped`` P6 audit-event on
        the FIRST drop and every Nth drop thereafter (#5515 — the second of
        the two sites this closes the gap for; see
        ``reyn.hooks.ingress._BoundedEventBridge._audit_drop``'s own
        docstring for the first). ``self._session`` is typed ``Any`` (a test
        double may not carry ``_audit_events``) — same getattr-guarded
        posture :meth:`submit` already uses for its own drain-task-death
        observer a few lines up, not a new idiom. Never raises (best-effort
        telemetry) and never includes the dropped ``template_vars`` —
        ``source`` (``_SOURCE_LABEL``, module-level — the SAME constant
        :meth:`submit` already passes ``observe_drain_task_death`` as its
        own ``label``, distinguishing this bridge's drops from
        ``_BoundedEventBridge``'s on the shared kind), ``point``, and the
        cumulative ``drop_count`` only."""
        audit_events = getattr(self._session, "_audit_events", None)
        if audit_events is None:
            return
        if self._drop_count != 1 and self._drop_count % _AUDIT_EVERY_N_DROPS != 0:
            return
        try:
            audit_events.emit(
                "ingress_bridge_dropped",
                source=_SOURCE_LABEL,
                point=point,
                drop_count=self._drop_count,
            )
        except Exception as exc:  # noqa: BLE001 — telemetry is best-effort
            logger.debug("_SessionFireBridge: emit_event(ingress_bridge_dropped) failed: %s", exc)

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
        from reyn.hooks.fold import drain_folded

        assert self._queue is not None

        async def _dispatch_raw_batch(raw_batch: "list[tuple[str, dict]]") -> None:
            # #5516 §1 item ①: read-and-reset atomically, right before the
            # dispatch(es) this count is FOR (mirrors
            # ingress._BoundedEventBridge's own identical pattern).
            skipped, self._dropped_since_last_batch = self._dropped_since_last_batch, 0
            # This bridge is per-SESSION, not per-adapter (unlike
            # _BoundedEventBridge) -- cron_fired and webhook_received share
            # it, so a raw batch can mix points. Group before dispatch,
            # same shape ComposedEventConsumer uses for its own
            # kind-mixing subscription queue. The FIRST group absorbs the
            # skip count (it is session-wide, not attributable to one
            # point either — see HookDispatcher._dispatch_batch_for_point's
            # own docstring for the same reasoning one level down); every
            # other group in this raw batch gets 0, so the count is
            # reported exactly once per batch, never duplicated per point.
            by_point: "dict[str, list[dict]]" = {}
            for point, template_vars in raw_batch:
                by_point.setdefault(point, []).append(template_vars)
            for i, (point, payloads) in enumerate(by_point.items()):
                try:
                    await self._session.dispatch_external_event_batch(
                        point, payloads, skipped_session_wide=(skipped if i == 0 else 0),
                    )
                except Exception:  # noqa: BLE001 — one bad dispatch must not kill the drain task
                    logger.warning(
                        "external-event hook dispatch failed for point=%r "
                        "(n=%d)", point, len(payloads), exc_info=True,
                    )

        await drain_folded(self._queue, _dispatch_raw_batch)


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
