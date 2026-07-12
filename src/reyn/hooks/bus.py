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

from reyn.hooks.event import HookEvent

logger = logging.getLogger(__name__)

# Default per-subscriber queue bound. A subscriber that falls this far behind
# starts losing the OLDEST unread events it hasn't drained yet (drop-newest
# would let a stuck subscriber silently wedge new events out of history
# indefinitely; drop-oldest bounds staleness instead) — same trade documented
# for the Ingress bridge's overflow path, adapted per-subscriber here since a
# Bus subscriber (unlike a single Sync drain task) is expected to poll at its
# own pace.
_DEFAULT_SUBSCRIBER_MAXSIZE = 128


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


class HookBus:
    """Per-Session pub/sub broadcast bus for :class:`HookEvent` (proposal
    §3.2/§3.3). One instance per Session — construct alongside that Session's
    ``HookDispatcher`` and inject it in (``HookDispatcher(..., bus=...)``);
    never share an instance across sessions (§3.3 v1 = per-Session, no
    cross-session correlation)."""

    def __init__(self, *, subscriber_maxsize: int = _DEFAULT_SUBSCRIBER_MAXSIZE) -> None:
        self._subscriber_maxsize = subscriber_maxsize
        self._subscribers: "list[asyncio.Queue[HookEvent]]" = []

    def subscribe(self) -> HookBusSubscription:
        """Register a new subscriber. Returns a handle whose ``get()``
        awaits the next broadcast event; call ``close()`` (or use as an
        async context manager) to stop receiving."""
        queue: "asyncio.Queue[HookEvent]" = asyncio.Queue(maxsize=self._subscriber_maxsize)
        self._subscribers.append(queue)
        return HookBusSubscription(bus=self, queue=queue)

    def _unsubscribe(self, queue: "asyncio.Queue[HookEvent]") -> None:
        try:
            self._subscribers.remove(queue)
        except ValueError:
            pass  # already removed / never registered — close() is idempotent

    def publish(self, event: HookEvent) -> None:
        """Broadcast ``event`` to every live subscriber. SYNCHRONOUS,
        non-blocking, never raises: each subscriber gets the SAME
        :class:`HookEvent` instance (no copy — proposal §3.2's "same instance,
        simultaneously" requirement), and a full subscriber queue drops its
        OLDEST unread entry to make room rather than blocking the publisher
        or dropping the newest broadcast (see module docstring). Zero
        subscribers → the loop body never runs (the no-subscriber
        byte-identical happy path)."""
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                try:
                    queue.get_nowait()  # drop the oldest to make room
                except asyncio.QueueEmpty:
                    pass
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


__all__ = ["HookBus", "HookBusSubscription"]
