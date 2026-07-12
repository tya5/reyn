"""reyn.hooks.bus — the per-Session ``HookBus`` (Hook-Event Redesign Phase 4a,
proposal ``docs/deep-dives/proposals/0059-hook-event-redesign.md`` §3.2/§3.3).

reyn's ``HookDispatcher`` (``reyn.hooks.dispatcher``) is the Sync path: an
awaited, per-hook-isolated, registration-ordered dispatch invoked at each of
the 10 builtin lifecycle/external points. Before this module reyn had no
pub/sub broadcast layer at all (proposal §3.2 reconcile) — this is that layer,
net-new, and the prerequisite substrate the Composer (Phase 4b) builds on.
Phase 4a delivers ONLY the Bus; it does not build the Composer, the
``emit_hook_event`` op (Phase 5), or ``QueuePolicy``/Backpressure.

**pub/sub broadcast, not a queue** (§3.2): ``publish`` hands the SAME
``HookEvent`` instance to every live subscriber. There is no "consume" —
one subscriber observing an event never removes it for another. Each
subscriber owns its OWN bounded queue so a slow subscriber cannot starve a
fast one, and ``publish`` never blocks the publisher (mirrors the Ingress
Adapter bridge's non-blocking, drop-newest-and-log overflow discipline,
``reyn.hooks.ingress._BoundedEventBridge`` — the same shape, one layer up).

**Independent of Sync dispatch** (§3.2): the same builtin kind (e.g.
``builtin:external:mcp_resource_updated``) can be Sync-registered for a
side-effect AND Bus-subscribed for observation — not mutually exclusive.
``HookDispatcher.dispatch`` publishes to its (optional) bus unconditionally,
regardless of whether any Sync hook is registered for the point, so a
Bus-only subscriber observes every dispatched event even with zero Sync
hooks configured.

**Per-Session scope (§3.3, v1)**: a ``HookBus`` is constructed once per
Session (mirroring ``HookDispatcher``'s own per-Session construction,
``runtime/session.py``) and injected into that Session's ``HookDispatcher``.
There is no cross-session Bus — a subscriber on one session's Bus can never
observe another session's events (session isolation is structural, not a
runtime check). Cross-session event correlation is an explicit v1 non-goal
(proposal §3.3/§11 future list).

**Band alignment (§3.2 reconcile)**: the Bus carries hook-events ONLY. It
does NOT route through the P6 ``EventLog`` subscriber path (audit-event) and
must never be confused with it (CLAUDE.md's 3-event rule — audit-event /
WAL-event / hook-event are three distinct things). Naming: ``HookBus`` (never
bare ``Bus`` or ``EventBus``) so it cannot collide with ``reyn.core.events``
at the identifier level, same discipline as ``HookEvent`` (``reyn.hooks.event``).

**No-subscriber happy path (byte-identical)**: ``publish`` with zero live
subscribers iterates an empty list — no queue, no task, no allocation beyond
the loop itself. Constructing a ``HookBus`` and never subscribing to it is
indistinguishable in behavior from not having one; ``HookDispatcher.dispatch``
without an injected bus (``bus=None``, the default) skips the publish call
entirely, so every pre-Phase-4a call site (and every pre-Phase-4a test) is
unaffected.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Callable

from reyn.hooks.event import HookEvent

logger = logging.getLogger(__name__)

EmitEvent = Callable[..., Any]

# Default per-subscriber queue bound. A subscriber that falls this far behind
# starts losing the OLDEST unread events it hasn't drained yet (drop-newest
# would let a stuck subscriber silently wedge new events out of history
# indefinitely; drop-oldest bounds staleness instead) — same trade documented
# for the Ingress bridge's overflow path, adapted per-subscriber here since a
# Bus subscriber (unlike a single Sync drain task) is expected to poll at its
# own pace.
_DEFAULT_SUBSCRIBER_MAXSIZE = 128

# Fail-visible drop cadence (#2886, Observability lens): a subscriber-queue
# overflow drop must never be silent, but ``publish`` is a sync/never-raises
# HOT PATH — auditing every single drop under sustained overflow would turn a
# slow-subscriber problem into an audit-log-flooding problem. Emit on the
# FIRST drop (so the correlation is reconstructable from ``reyn events`` the
# moment it starts) and then only every Nth drop thereafter (so a sustained
# overflow still leaves periodic breadcrumbs without one audit-event per
# broadcast). Mirrors the Composer's ``composer_dropped`` metadata-only
# discipline — never the event payload/content, only counters + an id.
_AUDIT_EVERY_N_DROPS = 100


class HookBusSubscription:
    """A single subscriber's handle on a :class:`HookBus`. Each subscription
    owns its own bounded queue (broadcast, not a shared consume-once queue —
    every live subscription independently receives every published
    :class:`HookEvent`). Use as an async context manager or call
    :meth:`close` explicitly when done observing."""

    def __init__(self, *, bus: "HookBus", queue: "asyncio.Queue[HookEvent]") -> None:
        self._bus = bus
        self._queue = queue
        self._closed = False

    async def get(self) -> HookEvent:
        """Await the next broadcast HookEvent for this subscription."""
        return await self._queue.get()

    def get_nowait(self) -> HookEvent:
        """Non-blocking variant of :meth:`get` — raises ``asyncio.QueueEmpty``
        if nothing has been broadcast to this subscription yet."""
        return self._queue.get_nowait()

    def close(self) -> None:
        """Detach from the bus. Idempotent — safe to call more than once."""
        if self._closed:
            return
        self._closed = True
        self._bus._unsubscribe(self._queue)

    async def __aenter__(self) -> "HookBusSubscription":
        return self

    async def __aexit__(self, *_exc: object) -> None:
        self.close()


@dataclass
class _SubscriberState:
    """Internal per-subscriber bookkeeping — the queue itself plus a stable
    ``subscriber_id`` (assigned at ``subscribe()`` time, never reused) and a
    monotonic drop counter (#2886) used to decide the first-drop/every-Nth
    audit cadence."""

    queue: "asyncio.Queue[HookEvent]"
    subscriber_id: int
    drop_count: int = 0


class HookBus:
    """Per-Session pub/sub broadcast bus for :class:`HookEvent` (proposal
    §3.2/§3.3). One instance per Session — construct alongside that Session's
    ``HookDispatcher`` and inject it in (``HookDispatcher(..., bus=...)``);
    never share an instance across sessions (§3.3 v1 = per-Session, no
    cross-session correlation)."""

    def __init__(
        self,
        *,
        subscriber_maxsize: int = _DEFAULT_SUBSCRIBER_MAXSIZE,
        emit_event: "EmitEvent | None" = None,
    ) -> None:
        self._subscriber_maxsize = subscriber_maxsize
        self._subscribers: "list[_SubscriberState]" = []
        self._next_subscriber_id = 0
        # #2886: metadata-only P6 audit-event sink for subscriber-queue-drop
        # visibility (mirrors HookDispatcher/Composer's own optional
        # ``emit_event`` — a plain ``(kind, **metadata)`` callable, never
        # required). ``None`` (the default) → drops are still counted
        # (``snapshot_drop_counts``) but never audited, matching every other
        # best-effort telemetry sink in this subsystem.
        self._emit_event = emit_event

    def subscribe(self) -> HookBusSubscription:
        """Register a new subscriber. Returns a handle whose ``get()``
        awaits the next broadcast event; call ``close()`` (or use as an
        async context manager) to stop receiving."""
        queue: "asyncio.Queue[HookEvent]" = asyncio.Queue(maxsize=self._subscriber_maxsize)
        subscriber_id = self._next_subscriber_id
        self._next_subscriber_id += 1
        self._subscribers.append(_SubscriberState(queue=queue, subscriber_id=subscriber_id))
        return HookBusSubscription(bus=self, queue=queue)

    def _unsubscribe(self, queue: "asyncio.Queue[HookEvent]") -> None:
        for i, state in enumerate(self._subscribers):
            if state.queue is queue:
                del self._subscribers[i]
                return
        # already removed / never registered — close() is idempotent

    def _audit_drop(self, state: "_SubscriberState") -> None:
        """Fire a metadata-only ``bus_subscriber_dropped`` P6 audit-event on
        the FIRST drop for this subscriber and every Nth drop thereafter
        (#2886). Never raises (best-effort telemetry, mirrors ``Composer.
        _audit``) and never includes the dropped event's kind/payload —
        subscriber id + cumulative drop count only."""
        if self._emit_event is None:
            return
        if state.drop_count != 1 and state.drop_count % _AUDIT_EVERY_N_DROPS != 0:
            return
        try:
            self._emit_event(
                "bus_subscriber_dropped",
                subscriber_id=state.subscriber_id,
                drop_count=state.drop_count,
            )
        except Exception as exc:  # noqa: BLE001 — telemetry is best-effort
            logger.debug("HookBus: emit_event(bus_subscriber_dropped) failed: %s", exc)

    def publish(self, event: HookEvent) -> None:
        """Broadcast ``event`` to every live subscriber. SYNCHRONOUS,
        non-blocking, never raises: each subscriber gets the SAME
        :class:`HookEvent` instance (no copy — proposal §3.2's "same instance,
        simultaneously" requirement), and a full subscriber queue drops its
        OLDEST unread entry to make room rather than blocking the publisher
        or dropping the newest broadcast (see module docstring). Zero
        subscribers → the loop body never runs (the no-subscriber
        byte-identical happy path).

        A drop is now fail-visible (#2886): it always increments that
        subscriber's ``drop_count`` (``snapshot_drop_counts``), and — on the
        first drop / every Nth drop thereafter, never every drop, to keep
        this hot path cheap under sustained overflow — fires a metadata-only
        ``bus_subscriber_dropped`` P6 audit-event via the optional
        ``emit_event`` sink."""
        for state in list(self._subscribers):
            queue = state.queue
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                try:
                    queue.get_nowait()  # drop the oldest to make room
                except asyncio.QueueEmpty:
                    pass
                state.drop_count += 1
                self._audit_drop(state)
                try:
                    queue.put_nowait(event)
                except asyncio.QueueFull:  # pragma: no cover — racing consumer, best-effort
                    logger.warning(
                        "HookBus: subscriber queue still full after drop-oldest — "
                        "broadcast of %r skipped for this subscriber", event.kind,
                    )

    @property
    def subscriber_count(self) -> int:
        """The number of currently-live subscriptions (public, non-private
        surface for tests/observability — not an internal-state pin)."""
        return len(self._subscribers)

    def snapshot_drop_counts(self) -> "dict[int, int]":
        """Public, snapshot-style read of every live subscriber's cumulative
        drop count, keyed by ``subscriber_id`` (#2886 — Observability lens,
        the fail-visible counter half of the fix; the audit-event is the
        other half). A copy, not a live view — safe for a test/observer to
        read without holding a reference into ``HookBus`` internals."""
        return {state.subscriber_id: state.drop_count for state in self._subscribers}


__all__ = ["HookBus", "HookBusSubscription"]
