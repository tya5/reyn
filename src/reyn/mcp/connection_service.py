"""MCPConnectionService — per-session held-open MCP connections (#2597 S2a).

Option C from the S2-pre spike (owner-delegated, do not relitigate): a persistent MCP
connection lives as a service INSIDE the agent's own session — not a separate driver
session. The spike proved the key precondition: FastMCP holds its client session in a
dedicated ``asyncio.Task``, so a client opened in task A is safely ``call_tool``'d from
task B on the SAME event loop. That is what makes holding connections open across
unrelated chat turns (which may run in different asyncio Tasks) safe, unlike
:class:`~reyn.mcp.pool.MCPClientPool` (a359 P2), whose ``get()`` fails fast off its
owning task because ITS contract is per-turn/task-affine by design.

Replaces the per-call open->close model on the live (non-ephemeral) session MCP path:
the pool opened + closed a fresh ``MCPClient`` (subprocess/HTTP session) on every single
tool call, which is correct but wasteful — this service opens each configured server
ONCE and reuses it for the rest of the session's lifetime.

Pool-compatible surface: ``get(server, config, *, agent_id=None) -> MCPClient`` matches
:meth:`MCPClientPool.get` byte-for-byte, so :class:`~reyn.mcp.gateway.MCPGateway` (the
one seam every MCP op flows through) works UNCHANGED when constructed with
``MCPGateway(pool=connection_service, ...)`` — it never has to know which kind of pool
it was handed.

Reconnect-on-demand (S2a-level resilience — deliberately NOT S2b's background health
loop / ping): a subprocess death or HTTP disconnect mid-session does NOT flip
``MCPClient.is_initialized()`` or the underlying ``fastmcp.Client.is_connected()``
(verified empirically against the real echo test server's ``die`` tool — both stay
True after the transport is gone) — the only observable signal is an exception raised
on the NEXT use. So the held-connection handle catches that signal, discards + reopens
the dead connection so the NEXT call lands on a healthy transport — a dropped
connection must not permanently wedge the server for the rest of the session.

#2597 F1 fix (post-S1 over-catch): ``MCPClient`` wraps EVERY exception (transport
death, application-level protocol errors, capability-gate refusals) into some
``MCPError`` subclass — so catching bare ``MCPError`` here would reconnect a perfectly
healthy connection on a capability-gate refusal or an app-level error (e.g. an unknown
tool/resource), needlessly killing+respawning a live stdio subprocess. ``_heal`` below
catches ONLY :class:`~reyn.mcp.client.MCPTransportError` — the narrower subclass
``reyn.mcp.client`` raises (via its ``_is_transport_death`` predicate, verified against
fastmcp 3.4.2 + the mcp SDK) exclusively for genuine transport-death. A
``MCPCapabilityError`` (gate refusal) or a plain ``MCPError`` (app-level) propagates
WITHOUT touching the connection.

CRITICAL — the reconnect must NOT silently retry a side-effectful call (at-most-once):
post-S1 ``call_tool`` raises ``MCPError`` on ANY transport failure, including the
drop-AFTER-execution window (the server RAN the tool, then the connection dropped before
its response arrived). Auto-retrying the same call on the fresh connection would
RE-EXECUTE the tool — an at-most-once → at-least-once regression vs the pre-S2a per-call
pool (a duplicated ``create_issue`` / ``send_email`` / counter increment). So the two op
classes are healed differently:

  - :meth:`_HeldConnection.call_tool` — **reconnect-then-propagate**: on ``MCPError``,
    heal the connection (reopen) but RE-RAISE the original error. The call is NOT
    retried, so a tool is executed at most once. The first ``call_tool`` right after an
    idle drop fails once; the healed connection makes every subsequent call succeed
    (S2b's proactive ping loop will detect the drop BEFORE the next call, delivering
    transparent healing SAFELY — S2a does not trade correctness for that UX).
  - :meth:`_HeldConnection.list_tools` — **retry-once**: an idempotent read is safe to
    re-run on the fresh connection, so it heals transparently (no user-visible failure).

Either way the fault the caller ultimately sees is contained by the existing MCPGateway
boundary into an LLM-visible error result, same as the pre-S2a per-call path.

Runtime-only state (S2a scope note): held connections are NOT WAL-derived / recoverable
state — they are reconstructed fresh (lazy-connect) after any process restart, exactly
like the pool's per-call clients were. Nothing here writes to the WAL.

#2597 S2b: because the connection stays open, FastMCP's ``session_task`` keeps its
receive loop running, so server-pushed notifications (tools/prompts ``list_changed``,
``notifications/progress``) arrive on the wire even between calls. ``emit_sink`` /
``tools_cache_invalidate`` (both optional; None = no bridge, byte-identical to pre-S2b
behaviour) are threaded down to a per-server :class:`~reyn.mcp.message_handler.
ReynMCPMessageHandler` built fresh each time :meth:`_ensure_open` opens (or reopens, on
reconnect) a held client — see that module for the notification->EventLog bridge design.

#2597 slice ②b — resource subscriptions (Q4, decided, do not relitigate): the
subscribed-URI set is RUNTIME-ONLY, in-memory, per server, held on THIS service
(``self._subscriptions``) — never WAL'd. A subscription carries no data of its own (MCP's
resources/subscribe is a thin "something changed, re-read if you care" signal, not a
message queue — see ``reyn.mcp.client.MCPClient.subscribe_resource``'s docstring), so it
is fully re-establishable and matches the gen-store runtime-only-state invariant. The
consequence: a fresh session (post-restart) starts with NO subscriptions (same as a fresh
``MCPClient`` starts with none), and a RECONNECT within the same live session (the F1
transport-death path) must explicitly RE-ISSUE ``subscribe_resource`` for every URI
tracked for that server on the fresh client — a brand-new ``mcp.ClientSession`` has no
memory of what the OLD (now-dead) session's client subscribed to. :meth:`_ensure_open`
does this re-subscribe immediately after opening a NEW client (whether that is the very
first open, where the tracked set is empty and the loop is a no-op, or a reconnect, where
it is the whole point) — see that method's inline comment.

#2608 H1 — external-event->hooks bridge (the first slice of the external-event arc):
``hook_trigger`` (optional, mirrors ``emit_sink``'s None-default no-op pattern) is an
ASYNC callable ``(point, template_vars) -> Awaitable`` — in practice a closure over the
owning session's ``HookDispatcher.dispatch``. It is never called directly from the MCP
receive-loop task (:class:`~reyn.mcp.message_handler.ReynMCPMessageHandler` runs
SYNCHRONOUSLY there and cannot ``await`` it — see that module's docstring). Instead this
service exposes :meth:`enqueue_external_event` — a SYNCHRONOUS, non-blocking
``put_nowait`` onto a BOUNDED ``asyncio.Queue`` (``_HOOK_EVENT_QUEUE_MAXSIZE`` entries)
— and drains it with a single background task (:meth:`_drain_hook_events`) running on
THIS service's (= the session's) event loop, which is what actually ``await``s
``hook_trigger``. Two invariants this buys:

  - **The receive loop never blocks and never stalls** on a slow/stuck hook — enqueue is
    O(1) and non-blocking; on overflow (a burst of resource updates arriving faster than
    hooks can be dispatched) the newest event is DROPPED + logged, never queued
    unboundedly and never backpressured onto the receive loop. This is the same
    "never stall / never delay other notification routing" discipline the module
    docstring establishes for the synchronous EventLog emit.
  - **Per-session dispatcher identity holds naturally**: because ``MCPConnectionService``
    is constructed per-session (see ``session.py``), the ``hook_trigger`` closure it is
    given targets THAT session's own ``HookDispatcher`` — a resource update on session A's
    held connection can only ever fire session A's hooks.

The drain task is lazily created on first ``enqueue_external_event`` call (mirrors the
lazy client-open pattern elsewhere in this class) and cancelled in :meth:`aclose`.
``hook_trigger=None`` (the ephemeral ``MCPClientPool`` path, or any session that never
wires one) → :meth:`enqueue_external_event` and the whole queue/drain-task machinery
never activate — byte-identical to pre-H1 behaviour.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

from reyn.mcp.client import MCPClient, MCPTransportError
from reyn.mcp.message_handler import EmitSink, ReynMCPMessageHandler, ToolsCacheInvalidate
from reyn.mcp.pool import describe_fault, is_real_control_flow

logger = logging.getLogger(__name__)

# #2608 H1: bound on the sync->async external-event bridge queue. Small and fixed —
# a burst of resource-update pushes beyond this is dropped (+logged), never queued
# unboundedly. Not currently exposed as config (H1 scope: prove the trigger mechanism;
# tuning the bound is a follow-up if a real workload needs it).
_HOOK_EVENT_QUEUE_MAXSIZE = 32

HookTrigger = Callable[[str, dict], Awaitable[Any]]


class MCPConnectionService:
    """Holds one open :class:`MCPClient` per configured server for the service's
    lifetime (= the owning agent session's lifetime). See module docstring for the
    Option C rationale, the pool-compatible ``get()`` contract, and the
    reconnect-on-demand design.

    Usage (mirrors ``MCPClientPool``, but no ``async with`` is required to use it —
    only :meth:`aclose` needs to run, at session teardown)::

        service = MCPConnectionService()
        client = await service.get("srv", cfg, agent_id="reyn/host")
        await client.call_tool("read_file", {"path": "x"})
        ...  # later turns, later tasks: the SAME connection is reused
        await service.aclose()  # session teardown — closes every held connection
    """

    def __init__(
        self,
        *,
        emit_sink: EmitSink | None = None,
        tools_cache_invalidate: ToolsCacheInvalidate | None = None,
        hook_trigger: "HookTrigger | None" = None,
        agent_name: str | None = None,
    ) -> None:
        # #2597 S2b: threaded into a fresh ReynMCPMessageHandler per held server
        # connection (see _ensure_open). None (default) = no notifications bridge —
        # the ephemeral per-call MCPClientPool path never constructs this service with
        # a sink, so it stays byte-identical to pre-S2b behaviour.
        self._emit_sink = emit_sink
        self._tools_cache_invalidate = tools_cache_invalidate
        # #2608 H1: the async closure over the owning session's HookDispatcher.dispatch.
        # None = no external-event hook bridge — see module docstring's H1 section.
        self._hook_trigger = hook_trigger
        self._agent_name = agent_name
        self._hook_event_queue: "asyncio.Queue[tuple[str, dict]] | None" = None
        self._hook_drain_task: "asyncio.Task | None" = None
        self._clients: dict[str, MCPClient] = {}
        # #2597 slice ②b: runtime-only, in-memory, NO WAL (Q4 — see module docstring).
        # server name -> set of URIs currently subscribed on that server's held
        # connection. Populated by _HeldConnection.subscribe_resource on success,
        # discarded by unsubscribe_resource, and consulted by _ensure_open to
        # re-subscribe every tracked URI on a fresh client (first open: empty, no-op;
        # reconnect: the whole point).
        self._subscriptions: dict[str, set[str]] = {}
        # One handle per server, cached so repeated get() calls for the same server
        # return the SAME object (connection-reuse identity) across the connection's
        # whole lifetime, including through a reconnect: the handle looks up the
        # live MCPClient by server name on every call rather than binding to one
        # MCPClient instance.
        self._handles: dict[str, "_HeldConnection"] = {}
        # Per-server lock so two concurrent first-use get() calls for the SAME server
        # (e.g. two chat turns racing on session startup) don't both open a client.
        self._locks: dict[str, asyncio.Lock] = {}

    def held_servers(self) -> list[str]:
        """Names of servers with a currently-open held connection. Read-only
        introspection for callers/tests — mirrors ``MCPClient.is_initialized()``'s
        public-surface pattern (never reach into ``_clients`` directly)."""
        return list(self._clients.keys())

    def _lock_for(self, server: str) -> asyncio.Lock:
        lock = self._locks.get(server)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[server] = lock
        return lock

    async def get(self, server: str, config: dict, *, agent_id: str | None = None) -> "MCPClient":
        """Return the held connection handle for ``server``, opening (and caching)
        it on first use. Pool-compatible signature — see module docstring.

        Unlike ``MCPClientPool.get()``, this is intentionally NOT task-affine: the
        spike proved a FastMCP client's session task makes cross-task use safe, so a
        held connection opened during one chat turn (one asyncio Task) is reused
        from a later turn running in a different Task without failing fast.
        """
        async with self._lock_for(server):
            await self._ensure_open(server, config, agent_id=agent_id)
            handle = self._handles.get(server)
            if handle is None:
                handle = _HeldConnection(self, server, config, agent_id)
                self._handles[server] = handle
            return handle  # type: ignore[return-value]  # duck-types MCPClient's call_tool/list_tools/is_initialized

    async def _ensure_open(
        self, server: str, config: dict, *, agent_id: str | None,
    ) -> MCPClient:
        """Return the live held client for ``server``, discarding + reopening a
        client that was explicitly closed out from under this service (defensive —
        the common dead-connection case is caught reactively by ``_HeldConnection``,
        not detected here; see module docstring)."""
        client = self._clients.get(server)
        if client is not None and not client.is_initialized():
            self._clients.pop(server, None)
            client = None
        if client is None:
            # #2597 S2b: a fresh handler per open (including every reconnect) — bound
            # to the server name closed over here, so a reconnected client's
            # notifications keep landing under the same server attribution.
            handler = None
            if self._emit_sink is not None:
                handler = ReynMCPMessageHandler(
                    self._emit_sink, server,
                    tools_cache_invalidate=self._tools_cache_invalidate,
                    # #2608 H1: wired only when a hook_trigger was injected (this
                    # service's enqueue_external_event is itself a no-op without one) —
                    # so a session with no hook_trigger stays byte-identical to pre-H1.
                    on_external_event=(
                        self.enqueue_external_event if self._hook_trigger is not None else None
                    ),
                    agent_name=self._agent_name,
                )
            client = MCPClient(
                config, agent_id=agent_id, message_handler=handler, server_name=server,
            )
            await client.__aenter__()  # initialize; held open (no matching __aexit__ until aclose/reconnect)
            self._clients[server] = client
            # #2597 capability/version gate: observability seam. This is the first
            # point in the live (non-ephemeral) session path that HAS the emit_sink
            # (the ephemeral per-call MCPClientPool path never wires one — see class
            # docstring — so it stays silent, matching pre-#2597 behaviour there).
            # Fires once per (re)connect, including reconnects (a version/capability
            # renegotiation is itself worth a trace event, not just the first
            # connect).
            if self._emit_sink is not None:
                self._emit_sink(
                    "mcp_initialized",
                    server=server,
                    negotiated_version=client.negotiated_version,
                    capabilities=client.advertised_capabilities(),
                )
            # #2597 slice ②b: re-issue subscribe_resource for every URI tracked for
            # THIS server on the fresh client. On the very first open the tracked set
            # is empty (nothing to do yet); on a reconnect (this same branch runs
            # because the dead client was already popped from self._clients by
            # _reconnect below) this is what makes a subscription survive a
            # transport-death reconnect — a brand-new mcp.ClientSession has no
            # memory of what the OLD session subscribed to. Per-URI try/except: a
            # server that no longer supports subscribe post-reconnect (or a single
            # bad URI) must not abort re-subscribing the REST of the tracked set,
            # and must not crash the reconnect itself.
            for uri in self._subscriptions.get(server, ()):
                try:
                    await client.subscribe_resource(uri)
                except Exception:  # noqa: BLE001 — one bad re-subscribe must not block the rest
                    logger.warning(
                        "MCPConnectionService: failed to re-subscribe %r on %r after "
                        "(re)connect", uri, server, exc_info=True,
                    )
        return client

    async def _reconnect(
        self, server: str, config: dict, *, agent_id: str | None,
    ) -> MCPClient:
        """Discard the (dead) held client for ``server`` and open a fresh one.
        Teardown of the dead client is best-effort — its transport is already gone,
        so a teardown fault here is expected and never blocks the reconnect."""
        old = self._clients.pop(server, None)
        if old is not None:
            try:
                await old.__aexit__(None, None, None)
            except BaseException as exc:  # noqa: BLE001 — best-effort; the connection is already dead
                if is_real_control_flow(exc):
                    raise
                logger.warning(
                    "MCPConnectionService: teardown of dead connection %r contained: %r",
                    server, exc,
                )
        return await self._ensure_open(server, config, agent_id=agent_id)

    def subscribed_uris(self, server: str) -> list[str]:
        """Sorted list of URIs currently tracked as subscribed for ``server``.
        Read-only introspection for callers/tests (mirrors :meth:`held_servers`'s
        public-surface pattern) — never reach into ``_subscriptions`` directly."""
        return sorted(self._subscriptions.get(server, ()))

    # ── #2608 H1: bounded sync->async external-event->hook bridge ──────────────────

    def enqueue_external_event(self, point: str, template_vars: dict) -> None:
        """SYNCHRONOUS, non-blocking entry point called from the MCP receive-loop task
        (``ReynMCPMessageHandler.on_resource_updated``). Never awaits, never raises,
        never blocks — see module docstring's H1 section for the full bridge design.

        No-op when ``hook_trigger`` is None (no hook wired for this session)."""
        if self._hook_trigger is None:
            return
        self._ensure_hook_drain_task()
        assert self._hook_event_queue is not None
        try:
            self._hook_event_queue.put_nowait((point, template_vars))
        except asyncio.QueueFull:
            # Bounded by construction (#2608 H1): a burst of resource updates faster
            # than hooks can be dispatched DROPS the newest event rather than growing
            # the queue unboundedly or blocking the receive loop.
            logger.warning(
                "MCPConnectionService: external-event hook queue full (maxsize=%d) — "
                "dropping %r event (server=%r)",
                _HOOK_EVENT_QUEUE_MAXSIZE, point, template_vars.get("server"),
            )

    def _ensure_hook_drain_task(self) -> None:
        if self._hook_event_queue is None:
            self._hook_event_queue = asyncio.Queue(maxsize=_HOOK_EVENT_QUEUE_MAXSIZE)
        if self._hook_drain_task is None or self._hook_drain_task.done():
            self._hook_drain_task = asyncio.create_task(self._drain_hook_events())

    async def _drain_hook_events(self) -> None:
        """Runs on the session's event loop (this service's owning loop — the same
        loop the held ``fastmcp.Client`` was opened on, see module docstring), so it
        can safely ``await hook_trigger(...)``. Per-event ``try/except``: a raising
        (or hanging-then-raising) hook dispatch must not kill the drain task — the
        NEXT queued event still gets a chance."""
        assert self._hook_event_queue is not None
        assert self._hook_trigger is not None
        while True:
            point, template_vars = await self._hook_event_queue.get()
            try:
                await self._hook_trigger(point, template_vars)
            except Exception:  # noqa: BLE001 — one bad dispatch must not kill the drain task
                logger.warning(
                    "MCPConnectionService: hook_trigger failed for %r", point, exc_info=True,
                )

    def _track_subscription(self, server: str, uri: str) -> None:
        self._subscriptions.setdefault(server, set()).add(uri)

    def _untrack_subscription(self, server: str, uri: str) -> None:
        self._subscriptions.get(server, set()).discard(uri)

    async def aclose(self) -> None:
        """Close every held connection. Idempotent — safe to call repeatedly (e.g. a
        session teardown seam that may run more than once)."""
        # #2608 H1: cancel the hook-event drain task FIRST (finally-guaranteed, not
        # except-Exception — CancelledError is a BaseException) so a client-teardown
        # fault below can never leave the drain task orphaned across session teardown.
        try:
            if self._hook_drain_task is not None and not self._hook_drain_task.done():
                self._hook_drain_task.cancel()
                try:
                    await self._hook_drain_task
                except asyncio.CancelledError:
                    pass
        finally:
            self._hook_drain_task = None

        clients = list(self._clients.items())
        self._clients.clear()
        self._handles.clear()
        for name, client in clients:
            try:
                await client.__aexit__(None, None, None)
            except BaseException as exc:  # noqa: BLE001 — fault isolation mirrors MCPClientPool.__aexit__
                if is_real_control_flow(exc):
                    raise
                logger.warning(
                    "MCPConnectionService: teardown of %r contained: %r",
                    name, describe_fault(exc),
                )

    async def __aenter__(self) -> "MCPConnectionService":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()


class _HeldConnection:
    """Duck-typed drop-in for :class:`MCPClient`, returned by
    :meth:`MCPConnectionService.get`. Exposes exactly the surface
    :class:`~reyn.mcp.gateway.MCPGateway` calls (``call_tool`` / ``list_tools`` /
    ``list_resources`` / ``list_resource_templates`` / ``read_resource`` /
    ``subscribe_resource`` / ``unsubscribe_resource`` / ``list_prompts`` /
    ``get_prompt`` / ``is_initialized``) so it's
    usable anywhere a bare ``MCPClient`` is expected.

    Looks up the currently-live held ``MCPClient`` by server name on every call
    instead of binding to one instance at construction time, so this handle's
    identity stays stable across the WHOLE connection lifetime — including through
    a reconnect (see :meth:`_with_reconnect`). A caller that stashed an earlier
    ``get()`` result keeps working after a reconnect without calling ``get()``
    again.
    """

    def __init__(
        self,
        service: MCPConnectionService,
        server: str,
        config: dict,
        agent_id: str | None,
    ) -> None:
        self._service = service
        self._server = server
        self._config = config
        self._agent_id = agent_id

    def is_initialized(self) -> bool:
        client = self._service._clients.get(self._server)
        return client is not None and client.is_initialized()

    async def call_tool(
        self,
        name: str,
        args: dict[str, Any],
        *,
        progress_callback: Any = None,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        # Reconnect-then-propagate (heal_only): a tool call is potentially
        # side-effectful, so on a transport MCPError we HEAL the connection (for the
        # next call) but RE-RAISE — never re-run the call — to preserve at-most-once
        # (a mid-execution drop must not double-execute the tool). See module docstring.
        return await self._heal(
            lambda c: c.call_tool(
                name, args, progress_callback=progress_callback, timeout_seconds=timeout_seconds,
            ),
            heal_only=True,
        )

    async def list_tools(self) -> list[dict[str, Any]]:
        # Retry-once: tools/list is an idempotent read, safe to re-run on the fresh
        # connection, so it heals transparently (no user-visible failure).
        return await self._heal(lambda c: c.list_tools(), heal_only=False)

    # #2597 slice ②a: resources consumption. All three are idempotent READS (no
    # server-side side effect), so — like list_tools, unlike side-effectful call_tool
    # — they heal with heal_only=False (retry-once on the fresh connection). A resource
    # read/list re-run after a mid-call transport drop is safe (at-most-once is not a
    # concern for a pure read), so the healed connection serves the retry transparently.
    async def list_resources(self) -> list[dict[str, Any]]:
        return await self._heal(lambda c: c.list_resources(), heal_only=False)

    async def list_resource_templates(self) -> list[dict[str, Any]]:
        return await self._heal(lambda c: c.list_resource_templates(), heal_only=False)

    async def read_resource(self, uri: str) -> dict[str, Any]:
        return await self._heal(lambda c: c.read_resource(uri), heal_only=False)

    # #2597 slice ②c: prompts consumption. Both are idempotent READS (no
    # server-side side effect), so — like list_resources/read_resource above —
    # they heal with heal_only=False (retry-once on the fresh connection).
    async def list_prompts(self) -> list[dict[str, Any]]:
        return await self._heal(lambda c: c.list_prompts(), heal_only=False)

    async def get_prompt(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        return await self._heal(lambda c: c.get_prompt(name, arguments), heal_only=False)

    # #2597 slice ②b: resource subscriptions. Unlike call_tool, subscribe/unsubscribe
    # are connection-MANAGEMENT operations, not data reads — but they still go through
    # _heal (heal_only=False) rather than a bespoke path: if the connection is dead,
    # heal reconnects it (which — via MCPConnectionService._ensure_open — re-issues
    # subscribe for every ALREADY-tracked URI, but NOT this one, since it is only
    # tracked below AFTER the call succeeds) and then _heal's heal_only=False retries
    # THIS call once on the fresh connection. That sequencing is what avoids a double
    # subscribe: the reconnect's re-subscribe loop and this method's own retry never
    # target the same URI in the same pass.
    async def subscribe_resource(self, uri: str) -> None:
        await self._heal(lambda c: c.subscribe_resource(uri), heal_only=False)
        self._service._track_subscription(self._server, uri)

    async def unsubscribe_resource(self, uri: str) -> None:
        await self._heal(lambda c: c.unsubscribe_resource(uri), heal_only=False)
        self._service._untrack_subscription(self._server, uri)

    async def _heal(
        self, op: "Callable[[MCPClient], Awaitable[Any]]", *, heal_only: bool,
    ) -> Any:
        """Run ``op`` against the currently-held client. On an :class:`MCPTransportError`
        — genuine transport-death, the only signal that actually means the held
        connection is dead (see module docstring + ``client.py``'s ``_is_transport_death``
        predicate) — discard + reopen the connection.

        #2597 F1: this deliberately catches ONLY ``MCPTransportError``, not the base
        ``MCPError``. Post-S1, every ``MCPClient`` method wraps ALL exceptions into some
        ``MCPError`` subclass, so a bare ``except MCPError:`` here used to over-catch two
        cases that are NOT a dead connection: a capability-gate refusal
        (``MCPCapabilityError`` — the server is alive, reyn just declined to send the
        request) and an application-level protocol error (unknown tool/resource, invalid
        params — the server responded, just with an error). Both of those propagate
        WITHOUT touching the connection now; only ``MCPTransportError`` triggers
        discard+reopen.

        ``heal_only=True`` (side-effectful ``call_tool``): re-raise the ORIGINAL error
        after healing — do NOT re-run ``op`` (preserves at-most-once; the healed
        connection serves the NEXT call). ``heal_only=False`` (idempotent ``list_tools``):
        retry ``op`` ONCE on the fresh connection; a second failure propagates unchanged
        (no silent retry loop)."""
        client = await self._service._ensure_open(
            self._server, self._config, agent_id=self._agent_id,
        )
        try:
            return await op(client)
        except MCPTransportError:
            fresh = await self._service._reconnect(
                self._server, self._config, agent_id=self._agent_id,
            )
            if heal_only:
                raise  # at-most-once: connection healed for the next call, but this call is NOT retried
            return await op(fresh)
