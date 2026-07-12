"""reyn.hooks.ingress — the unified Ingress Adapter interface (Hook-Event
Redesign Phase 2, proposal ``docs/deep-dives/proposals/0059-hook-event-
redesign.md`` §6).

Before Phase 2, reyn's 4 external-event sources converged on
``HookDispatcher.dispatch``/``Session.dispatch_external_event`` through TWO
different, independently-implemented ingress patterns (#2608 H1/H4/H5):

  - **in-process bridge** (``mcp_resource_updated``, ``file_changed``): the raw
    signal arrives INSIDE the session's own process (an asyncio task for MCP,
    a foreign OS thread for the fs watcher) and is handed off through a
    bounded ``asyncio.Queue`` + a lazily-started drain task that ``await``s a
    ``hook_trigger`` closure already bound to THIS session's
    ``HookDispatcher.dispatch`` (captured at session-construction time —
    ``runtime/session.py``'s ``MCPConnectionService``/``FsWatcher`` wiring).
    Before Phase 2 this exact bounded-queue-plus-drain-task shape was
    duplicated almost verbatim in ``mcp/connection_service.py`` and
    ``runtime/fs_watcher.py``.
  - **out-of-process resolve+fire** (``cron_fired``, ``webhook_received``):
    the raw signal arrives OUTSIDE any session's process context (the
    web-server's cron runner / the webhook gateway), so there is no
    already-bound ``hook_trigger`` closure to call — the target Session must
    first be RESOLVED (get-or-spawn) from the ``AgentRegistry``, then the hook
    is fired via ``reyn.hooks.external_fire.fire_and_forget`` (a background
    ``asyncio.create_task`` so a slow hook action never stalls the cron job's
    own delivery or the webhook plugin's HTTP response).

This module unifies both patterns behind ONE ``IngressAdapter`` interface:

    (raw signal) --to_event()--> HookEvent --deliver()--> the resolved
    Session's HookDispatcher

``to_event`` is a PURE conversion (uses Phase 1's ``schema_registry.
build_hook_payload``, so the field-for-field schemas + the Phase-1 sync-gate
stay intact) — it does no I/O and resolves nothing. ``deliver`` closes the
per-pattern delivery mechanism (bounded-queue-bridge for in-process,
Session-resolve-then-fire-and-forget for out-of-process) — Sync dispatch
(``HookDispatcher.dispatch``) and any future Async Bus never see these
internals; they only ever receive ``(bare_point, payload)`` via the existing
``hook_trigger``/``dispatch_external_event`` seam, UNCHANGED.

Deliberately NOT unified: the raw-signal SHAPE each adapter's ``to_event``
accepts (an MCP notification's uri, a filesystem path+event_type, a cron
job_name+to, a webhook sender string) — these are inherently different
external protocols/signals, and forcing one shared signature would either
lose information or resurrect an untyped catch-all dict (the exact ad-hoc
shape Phase 1 eliminated). What IS unified is the two-step shape
(``to_event`` then ``deliver``) and the return type of ``to_event``
(``HookEvent``, always).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable, Protocol, runtime_checkable

from reyn.hooks.event import HookEvent
from reyn.hooks.schema_registry import bare_point, build_hook_payload, canonical_kind

logger = logging.getLogger(__name__)

HookTrigger = Callable[[str, dict], Awaitable[Any]]


@runtime_checkable
class IngressAdapter(Protocol):
    """The unified ingress interface every external source's adapter
    implements. ``to_event`` is pure (no I/O, no Session resolve);
    ``deliver`` performs the actual hand-off to the resolved Session's
    ``HookDispatcher`` (queued-and-drained for in-process adapters,
    resolved-then-fired for out-of-process adapters — see module docstring).
    """

    def to_event(self, *args: Any, **kwargs: Any) -> HookEvent:
        ...

    def deliver(self, event: HookEvent, *args: Any, **kwargs: Any) -> Any:
        ...


# ---------------------------------------------------------------------------
# In-process bridge (§6: MCP + Fs share this — the bounded-queue-plus-drain
# shape consolidated out of connection_service.py / fs_watcher.py duplication)
# ---------------------------------------------------------------------------


class _BoundedEventBridge:
    """The in-process ingress delivery mechanism: a bounded ``asyncio.Queue``
    + a lazily-created background drain task that ``await``s ``hook_trigger``
    for each queued :class:`HookEvent`, one at a time, with per-event
    ``try/except`` (one bad dispatch must never kill the drain loop — mirrors
    ``HookDispatcher.dispatch``'s own per-hook isolation one level up).

    The THREAD/task hand-off that gets a raw signal safely onto the session's
    own event loop (``call_soon_threadsafe`` for the fs watcher's foreign
    watchdog-thread origin; a plain synchronous call for MCP's same-loop
    receive-task origin) is NOT this bridge's concern — that happens BEFORE
    :meth:`deliver` is called, in each adapter's own producer callback. This
    bridge only owns what happens once already running on the session's loop:
    bound the queue, drop-newest-and-log on overflow, drain.

    ``hook_trigger=None`` (no hook wired for this session) makes
    :meth:`deliver` a no-op and the whole queue/drain-task machinery never
    activates — byte-identical to a build with no hook mechanism at all.
    """

    def __init__(
        self,
        *,
        hook_trigger: "HookTrigger | None",
        maxsize: int,
        adapter_name: str,
    ) -> None:
        self._hook_trigger = hook_trigger
        self._maxsize = maxsize
        self._adapter_name = adapter_name
        self._queue: "asyncio.Queue[HookEvent] | None" = None
        self._drain_task: "asyncio.Task | None" = None

    def deliver(self, event: HookEvent) -> None:
        """SYNCHRONOUS, non-blocking. Never awaits, never raises."""
        if self._hook_trigger is None:
            return
        self._ensure_drain_task()
        assert self._queue is not None
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            # Bounded by construction: a burst faster than hooks can be
            # dispatched drops the NEWEST event rather than growing the queue
            # unboundedly or blocking the producer.
            logger.warning(
                "%s: hook-event queue full (maxsize=%d) — dropping %r event",
                self._adapter_name, self._maxsize, event.kind,
            )

    def _ensure_drain_task(self) -> None:
        if self._queue is None:
            self._queue = asyncio.Queue(maxsize=self._maxsize)
        if self._drain_task is None or self._drain_task.done():
            self._drain_task = asyncio.create_task(self._drain())

    async def _drain(self) -> None:
        assert self._queue is not None
        assert self._hook_trigger is not None
        while True:
            event = await self._queue.get()
            try:
                await self._hook_trigger(bare_point(event.kind), event.payload)
            except Exception:  # noqa: BLE001 — one bad dispatch must not kill the drain task
                logger.warning(
                    "%s: hook_trigger failed for %r", self._adapter_name, event.kind,
                    exc_info=True,
                )

    async def aclose(self) -> None:
        """Cancel the drain task. Idempotent — safe to call repeatedly and
        safe even if :meth:`deliver` was never called (no task started)."""
        if self._drain_task is not None and not self._drain_task.done():
            self._drain_task.cancel()
            try:
                await self._drain_task
            except asyncio.CancelledError:
                pass
        self._drain_task = None


class McpIngressAdapter:
    """§6.1 MCP Adapter — standard ``resources/updated`` notification only (no
    custom dialect). Converts a resource-update signal into the builtin
    ``mcp_resource_updated`` :class:`HookEvent` (via Phase 1's
    ``build_hook_payload``, so the field-set stays schema-gated) and delivers
    it through the shared in-process bridge."""

    def __init__(self, *, hook_trigger: "HookTrigger | None", maxsize: int = 32) -> None:
        self._bridge = _BoundedEventBridge(
            hook_trigger=hook_trigger, maxsize=maxsize, adapter_name="McpIngressAdapter",
        )

    def to_event(
        self, uri: "str | None", *, server: str, agent_name: "str | None", resync: bool,
    ) -> HookEvent:
        payload = build_hook_payload(
            "mcp_resource_updated", server=server, uri=uri, agent_name=agent_name, resync=resync,
        )
        return HookEvent(kind=canonical_kind("mcp_resource_updated"), payload=payload)

    def deliver(self, event: HookEvent) -> None:
        self._bridge.deliver(event)

    async def aclose(self) -> None:
        await self._bridge.aclose()


class FsIngressAdapter:
    """§6.3 Fs Adapter — watchdog → ``file_changed``. Debounce-per-path stays
    the caller's responsibility (``runtime/fs_watcher.py``'s
    ``_FsEventHandler._maybe_fire``, upstream of this adapter — a debounced-
    away event never reaches :meth:`to_event` at all). SECURITY invariant
    preserved: this adapter has no widening capability of its own — the
    watched-paths OUT-set is entirely owned by ``FsWatcher``/
    ``FsWatchConfig`` (restart-only, no op/tool call reaches it)."""

    def __init__(self, *, hook_trigger: "HookTrigger | None", maxsize: int = 32) -> None:
        self._bridge = _BoundedEventBridge(
            hook_trigger=hook_trigger, maxsize=maxsize, adapter_name="FsIngressAdapter",
        )

    def to_event(self, path: str, event_type: str) -> HookEvent:
        payload = build_hook_payload("file_changed", path=path, event_type=event_type)
        return HookEvent(kind=canonical_kind("file_changed"), payload=payload)

    def deliver(self, event: HookEvent) -> None:
        self._bridge.deliver(event)

    async def aclose(self) -> None:
        await self._bridge.aclose()


# ---------------------------------------------------------------------------
# Out-of-process resolve+fire (§6: Cron + Webhook share this shape — Session
# resolve is CLOSED here, never leaked to a Sync-dispatch/Bus caller)
# ---------------------------------------------------------------------------


class CronIngressAdapter:
    """§6.4 Cron Adapter — internal-scheduler source (NOT an external
    protocol; reconciled in the proposal as its own "internal scheduler
    source" classification, §6). Resolves the fired job's own persistent
    ``cron:<job_name>`` Session from the ``AgentRegistry`` (out-of-process
    pattern — the web-server cron runner has no already-bound
    ``hook_trigger`` closure to call), then fires ``cron_fired`` via
    ``fire_and_forget`` so a slow hook action never stalls the job's own
    inbox delivery.
    """

    TRANSPORT = "cron"

    @staticmethod
    def session_id(job_name: str) -> str:
        return f"{CronIngressAdapter.TRANSPORT}:{job_name}"

    def resolve_session(self, registry: Any, agent_name: str, job_name: str) -> Any:
        """Get-or-spawn the persistent ``cron:<job_name>`` Session of
        ``agent_name`` and boot its run-loop — the out-of-process
        Session-resolve, closed inside this adapter (never leaked to
        Sync dispatch / a future Async Bus)."""
        session = registry.resolve_session(agent_name, self.TRANSPORT, job_name)
        registry.ensure_session_running(agent_name, self.session_id(job_name))
        return session

    def to_event(self, job_name: str, to: str) -> HookEvent:
        payload = build_hook_payload("cron_fired", job_name=job_name, to=to)
        return HookEvent(kind=canonical_kind("cron_fired"), payload=payload)

    def deliver(self, event: HookEvent, session: Any) -> None:
        from reyn.hooks.external_fire import fire_and_forget
        fire_and_forget(session, bare_point(event.kind), event.payload)


class WebhookIngressAdapter:
    """§6.2 Webhook Adapter — provider schema, unknown opaque (namespace-
    per-provider is a later phase; Phase 2 keeps the single ``webhook_received``
    builtin kind, unchanged), signature verify stays at the gateway plugin
    layer (unchanged — Phase 2 does not move where verification happens).
    SECURITY invariant preserved: the delivered payload carries ONLY routing
    metadata (``transport``/``sender``) — the raw inbound request body is
    NEVER included (token/PII never reaches a hook's template_vars)."""

    _GENERIC_TRANSPORT = "webhook"

    @staticmethod
    def parse_sender(sender: str) -> "tuple[str, str]":
        transport, sep, external_id = sender.partition(":")
        if not sep or not transport.strip():
            return WebhookIngressAdapter._GENERIC_TRANSPORT, sender
        return transport, external_id

    def resolve_session(self, registry: Any, agent_name: str, sender: str) -> Any:
        """Get-or-spawn the per-sender webhook Session and boot its run-loop
        — the out-of-process Session-resolve, closed inside this adapter."""
        transport, native_id = self.parse_sender(sender)
        session = registry.resolve_session(agent_name, transport, native_id)
        registry.ensure_session_running(agent_name, f"{transport}:{native_id}")
        return session

    def to_event(self, sender: str) -> HookEvent:
        transport, _external_id = self.parse_sender(sender)
        # SECURITY (preserved from pre-Phase-2 dispatch_webhook_received): only
        # transport + sender — routing metadata already used for dispatch
        # attribution — never the raw inbound body/text.
        payload = build_hook_payload("webhook_received", transport=transport, sender=sender)
        return HookEvent(kind=canonical_kind("webhook_received"), payload=payload)

    def deliver(self, event: HookEvent, session: Any) -> None:
        from reyn.hooks.external_fire import fire_and_forget
        fire_and_forget(session, bare_point(event.kind), event.payload)


__all__ = [
    "CronIngressAdapter",
    "FsIngressAdapter",
    "IngressAdapter",
    "McpIngressAdapter",
    "WebhookIngressAdapter",
]
