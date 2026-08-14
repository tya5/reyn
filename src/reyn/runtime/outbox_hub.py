"""OutboxHub — single-drain broadcast fan-out for ``session.outbox`` (ADR-0039 P6b).

`session.outbox` is a single-consumer :class:`asyncio.Queue`: every ``.get()``
hands an item to exactly ONE getter. Before this hub, two code paths drained it
directly — the registry's local ``_forwarder`` (→ ``repl_outbox``, the local REPL
sink) and the AG-UI ``_SessionFrameSource`` (one drain per SSE connection). They
were kept mutually exclusive (``ensure_running`` starts the forwarder;
``ensure_session_running`` does not and lets the AG-UI caller drain) precisely
because two concurrent getters on one Queue *steal* frames from each other, so N
AG-UI surfaces would each see only a fraction of the stream.

P6b makes AG-UI the sole UI transport for the browser AND the ``--connect`` thin
client concurrently, so the mutual-exclusion no longer holds. This hub resolves
it structurally:

- **ONE** task drains ``session.outbox`` — the sole ``.get()`` consumer.
- Each surface calls :meth:`OutboxHub.subscribe` to get its OWN
  :class:`HubSubscription` queue; the hub fans every message out to every
  subscription, so each surface receives the FULL stream in order.
- The local ``_forwarder`` and the AG-UI ``_SessionFrameSource`` become hub
  *subscribers* rather than direct outbox drainers — folding the two paths into
  one non-stealing fan-out.

**Slow / dead surface never blocks the writer.** Fan-out uses ``put_nowait``.
A subscription may cap its queue (``maxsize``); when a bounded surface's queue is
full (it stopped draining — a stuck SSE client) the hub *disconnects* it
(disconnect-slow policy): it clears that one queue, pushes a close sentinel so the
subscriber's :meth:`HubSubscription.get` returns ``None``, and drops it from the
fan-out set. The single drain task therefore never awaits a full queue and other
surfaces are unaffected. The local REPL subscription is created *unbounded*
(``maxsize=0``) so its delivery is byte-identical to the pre-hub direct forwarder
(a fast, unbounded local sink is never disconnected).

``__end__`` (the session-shutdown terminal) fans out to all surfaces like any
other message; each subscriber terminates its own loop on ``kind == "__end__"``.
The drain task then returns; a later :meth:`subscribe` restarts it (session
restart), since the underlying ``session.outbox`` Queue is reused.
"""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from reyn.runtime.outbox import OutboxMessage
    from reyn.runtime.tracked_tasks import TrackedTaskSet

logger = logging.getLogger(__name__)

# Default per-surface queue cap for a *bounded* subscription (a remote SSE
# surface). Generous — a surface this far behind is genuinely stuck, not merely
# briefly slow — so disconnect-slow only fires on a truly dead reader.
DEFAULT_SURFACE_MAXSIZE = 4096

# Distinct from ``__end__`` (a real terminal OutboxMessage): this private
# sentinel signals a subscription was force-closed by disconnect-slow, surfaced
# to the consumer as ``get()`` returning ``None``.
_CLOSED = object()


class HubSubscription:
    """One surface's view of the broadcast stream — a private queue fed by the hub.

    Consumers loop on :meth:`get`, which yields each :class:`OutboxMessage` in
    order and returns ``None`` once the subscription is force-closed
    (disconnect-slow). ``__end__`` arrives as an ordinary message (``kind ==
    "__end__"``); ``None`` is *only* the disconnected-surface signal.
    """

    __slots__ = ("_hub", "_queue")

    def __init__(self, hub: "OutboxHub", maxsize: int) -> None:
        self._hub = hub
        # maxsize=0 → unbounded (the local REPL sink, byte-identical to the
        # pre-hub direct forwarder). A positive cap arms disconnect-slow.
        self._queue: "asyncio.Queue" = asyncio.Queue(maxsize=maxsize)

    async def get(self) -> "OutboxMessage | None":
        """Next message in order, or ``None`` if this surface was disconnected."""
        item = await self._queue.get()
        if item is _CLOSED:
            return None
        return item

    def close(self) -> None:
        """Detach this surface from the hub (graceful client teardown)."""
        self._hub._remove(self)


class OutboxHub:
    """Single-drain broadcast hub over one source ``asyncio.Queue``."""

    def __init__(
        self, source: "asyncio.Queue", *, name: str = "",
        task_tracker: "TrackedTaskSet",
    ) -> None:
        self._source = source
        self._subs: "set[HubSubscription]" = set()
        self._drain_task: "asyncio.Task | None" = None
        self._name = name
        # #4759: register the drain task with the owning Session's task
        # funnel too (tracked_tasks.py) -- __end__ (put by Session.run()'s
        # own finally on every exit path) reaches this loop's queue.get()
        # and lets it return, but that PROCESSING happens in this separate
        # task, which is not otherwise joined by anything -- registering it
        # is what lets registry.shutdown() actually wait for that to happen
        # instead of returning while it's still catching up. REQUIRED (no
        # None-tolerant fallback): the one production construction site
        # (session.py) always supplies one, and an optional fallback here
        # would silently recreate #4759's own defect shape at a future
        # construction site that forgets to pass one.
        self._task_tracker = task_tracker

    def subscribe(self, *, maxsize: int = 0) -> HubSubscription:
        """Register a new surface. ``maxsize=0`` is unbounded (local sink);
        a positive cap arms the disconnect-slow policy for a remote surface.

        Lazily (re)starts the single drain task, so the hub needs no running
        loop at construction — only when the first surface attaches."""
        sub = HubSubscription(self, maxsize)
        self._subs.add(sub)
        self._ensure_drain()
        return sub

    def _remove(self, sub: HubSubscription) -> None:
        self._subs.discard(sub)

    def has_subscribers(self) -> bool:
        """Whether any surface currently holds a live subscription.

        #3793 stage 2: the emission-time gate a producer uses to decide
        whether a transient (status/trace) message is worth queuing at all —
        distinct from ``_fanout``'s per-message no-op, which only applies
        AFTER a message has already reached ``_drain`` (itself only running
        once ``subscribe()`` has been called at least once). A session booted
        via ``ensure_session_running`` (no forwarder — e.g. a persistent
        ``cron:``/``webhook:`` session, FP-0043) may never be subscribed to
        at all; without this earlier gate, ``_drain`` never starts and
        ``session.outbox`` (the source queue) grows without bound for the
        life of the session."""
        return bool(self._subs)

    def _ensure_drain(self) -> None:
        if self._drain_task is None or self._drain_task.done():
            self._drain_task = self._task_tracker.spawn(
                self._drain(), disposition="cancel_join", name=f"outbox-drain-{self._name}",
            )

    async def _drain(self) -> None:
        """The SOLE ``source.get()`` consumer: drain and fan out forever, until
        the ``__end__`` terminal (session shutdown)."""
        while True:
            msg = await self._source.get()
            self._fanout(msg)
            if getattr(msg, "kind", None) == "__end__":
                return

    def _fanout(self, msg: "OutboxMessage") -> None:
        # Never awaits: put_nowait into each surface's queue; a full bounded
        # queue triggers disconnect-slow rather than blocking the drain.
        for sub in list(self._subs):
            try:
                sub._queue.put_nowait(msg)
            except asyncio.QueueFull:
                self._disconnect_slow(sub)

    def _disconnect_slow(self, sub: HubSubscription) -> None:
        """Drop a stuck surface without blocking the writer or other surfaces.

        Runs synchronously (no ``await``) so it is atomic against the drain
        loop and the subscriber's ``get()``: clear the surface's queue, push the
        close sentinel (so its next ``get()`` returns ``None``), and remove it
        from the fan-out set. A dead reader thus cannot back-pressure the hub."""
        self._subs.discard(sub)
        while True:
            try:
                sub._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        try:
            sub._queue.put_nowait(_CLOSED)
        except asyncio.QueueFull:  # pragma: no cover — just cleared above
            pass
        logger.warning(
            "outbox_hub[%s]: disconnected a slow surface (queue full, %d remain)",
            self._name, len(self._subs),
        )


__all__ = ["OutboxHub", "HubSubscription", "DEFAULT_SURFACE_MAXSIZE"]
