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
True after the transport is gone) — the only observable signal is an :class:`MCPError`
raised on the NEXT use. So the held-connection handle catches an ``MCPError``, discards
+ reopens the dead connection so the NEXT call lands on a healthy transport — a dropped
connection must not permanently wedge the server for the rest of the session.

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
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

from reyn.mcp.client import MCPClient, MCPError
from reyn.mcp.message_handler import EmitSink, ReynMCPMessageHandler, ToolsCacheInvalidate
from reyn.mcp.pool import describe_fault, is_real_control_flow

logger = logging.getLogger(__name__)


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
    ) -> None:
        # #2597 S2b: threaded into a fresh ReynMCPMessageHandler per held server
        # connection (see _ensure_open). None (default) = no notifications bridge —
        # the ephemeral per-call MCPClientPool path never constructs this service with
        # a sink, so it stays byte-identical to pre-S2b behaviour.
        self._emit_sink = emit_sink
        self._tools_cache_invalidate = tools_cache_invalidate
        self._clients: dict[str, MCPClient] = {}
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
                )
            client = MCPClient(config, agent_id=agent_id, message_handler=handler)
            await client.__aenter__()  # initialize; held open (no matching __aexit__ until aclose/reconnect)
            self._clients[server] = client
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

    async def aclose(self) -> None:
        """Close every held connection. Idempotent — safe to call repeatedly (e.g. a
        session teardown seam that may run more than once)."""
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
    ``is_initialized``) so it's usable anywhere a bare ``MCPClient`` is expected.

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

    async def _heal(
        self, op: "Callable[[MCPClient], Awaitable[Any]]", *, heal_only: bool,
    ) -> Any:
        """Run ``op`` against the currently-held client. On an :class:`MCPError` —
        the only observable signal of a dead connection (see module docstring) —
        discard + reopen the connection.

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
        except MCPError:
            fresh = await self._service._reconnect(
                self._server, self._config, agent_id=self._agent_id,
            )
            if heal_only:
                raise  # at-most-once: connection healed for the next call, but this call is NOT retried
            return await op(fresh)
