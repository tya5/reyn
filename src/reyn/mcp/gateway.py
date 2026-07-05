"""MCPGateway — the single seam every MCP operation flows through (#2421).

Root cause of the recurring MCP crashes: fault-isolation + task-affine lifecycle + per-call timeout
were applied PER entry path (op-call got them; list/probe were swept-missed → the list-path crash
recurred). This gateway consolidates them in ONE place so a new entry path is a thin caller by
construction and cannot re-introduce a crash/leak:

  [1] structured sub-task join  — the SDK stdio_client/ClientSession task group joins its internal
      reader/writer tasks at ``close`` (in the pool's owning task); a sub-task fault surfaces
      synchronously in-scope, never orphaning to the loop ("Task exception was never retrieved").
  [2] contain-all boundary      — ``except BaseException``; re-raise ONLY genuine control flow
      (``is_real_control_flow``: KeyboardInterrupt/SystemExit, or CancelledError while THIS task is
      actually cancelling); contain everything else (transport faults, groups, spurious internal
      cancels) as an :class:`MCPFault`. Exception-structure-independent — a bare cancel-mixed
      ``BaseExceptionGroup`` from a dead subprocess is contained, not propagated.
  [3] pool task-affinity        — clients open + close on one task via :class:`MCPClientPool`.
  [4] per-call timeout          — a finite bound per op (``call_timeout_seconds``; ``<= 0`` opts out).

The gateway raises ONLY :class:`MCPFault` (an ``Exception``) or genuine control flow — never a bare
``BaseExceptionGroup`` — so thin callers need only ``except MCPFault`` (or ``except Exception``) to
shape their surface (list → ``[{"error": …}]``, call → error result, probe → status). Result
SHAPING (media blocks, offload marker) is pure post-processing and stays in the caller.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from reyn.mcp.client import MCPClient
from reyn.mcp.pool import MCPClientPool, describe_fault, is_real_control_flow

if TYPE_CHECKING:
    from reyn.mcp.connection_service import MCPConnectionService

# Generous finite default (owner S3): a hung server must not wedge the run, but the default is
# lenient; operators tighten per server via ``call_timeout_seconds`` (``<= 0`` opts out).
_DEFAULT_MCP_CALL_TIMEOUT_SECONDS: float = 120.0


class MCPFault(Exception):
    """A contained MCP fault (transport / protocol / teardown), summarised for an LLM-facing result.

    The gateway converts ANY non-control-flow fault into this so callers never see a bare
    ``BaseExceptionGroup``. Its ``str`` is the aggregated fault text from :func:`describe_fault`."""


def resolve_call_timeout(config: dict) -> "float | None":
    """Per-call timeout: the finite default, overridden by a per-server ``call_timeout_seconds``
    (float); ``<= 0`` opts out (no bound). A malformed value falls back to the default (fail-safe —
    keep a finite bound)."""
    ct = config.get("call_timeout_seconds")
    timeout: "float | None" = _DEFAULT_MCP_CALL_TIMEOUT_SECONDS
    if ct is not None:
        try:
            timeout = float(ct)
        except (TypeError, ValueError):
            timeout = _DEFAULT_MCP_CALL_TIMEOUT_SECONDS
    if timeout is not None and timeout <= 0:
        return None
    return timeout


class MCPGateway:
    """The one object that touches :class:`MCPClient`. All MCP ops (list / call / probe) run here.

    Pass an existing ``pool`` to REUSE cached clients across calls (op-call path); omit it for a
    one-shot op (list / probe) — the gateway opens and closes its own pool for that call.

    ``pool`` accepts anything with a pool-compatible ``get(server, config, *, agent_id=None)``:
    :class:`~reyn.mcp.pool.MCPClientPool` (per-turn, task-affine) or
    :class:`~reyn.mcp.connection_service.MCPConnectionService` (#2597 S2a — held open for the
    whole session, cross-task-safe). The gateway itself never constructs either; it only calls
    ``get()`` on whatever the caller injects.
    """

    def __init__(
        self,
        *,
        pool: "MCPClientPool | MCPConnectionService | None" = None,
        agent_id: str | None = None,
    ) -> None:
        self._injected_pool = pool
        self._agent_id = agent_id

    @asynccontextmanager
    async def _acquire(self, server: str, config: dict):
        """Yield an open client for ``server``. Reuses the injected pool, or opens+closes a private
        one-shot pool (whose ``__aexit__`` closes in-task and contains teardown faults)."""
        if self._injected_pool is not None:
            yield await self._injected_pool.get(server, config, agent_id=self._agent_id)
        else:
            async with MCPClientPool() as pool:
                yield await pool.get(server, config, agent_id=self._agent_id)

    async def _run(
        self, server: str, config: dict, op: Callable[[MCPClient], Awaitable[Any]],
        *, timeout: "float | None",
    ) -> Any:
        """Open a client and run ``op`` inside the contain-all boundary + a finite timeout. Raises
        :class:`MCPFault` for any non-control-flow fault (incl. teardown groups); re-raises genuine
        control flow untouched.

        The timeout wraps the WHOLE ``acquire + op`` — the OPEN (initialize handshake) as well as the
        call — so a server that HANGS ON INIT is bounded too (else a fresh one-shot open for a
        list/probe would hang unbounded). On timeout ``asyncio.timeout`` surfaces a ``TimeoutError``
        (an Exception, our task not cancelling), which the boundary contains as an ``MCPFault``."""
        try:
            if timeout is not None:
                async with asyncio.timeout(timeout):
                    async with self._acquire(server, config) as client:
                        return await op(client)
            else:
                async with self._acquire(server, config) as client:
                    return await op(client)
        except MCPFault:
            raise
        except BaseException as exc:  # noqa: BLE001 — the seam contains ALL non-control-flow faults
            if is_real_control_flow(exc):
                raise
            raise MCPFault(describe_fault(exc)) from exc

    async def list_tools(self, server: str, config: dict) -> list[dict]:
        """Return the server's advertised tools. Raises :class:`MCPFault` on any contained fault."""
        return await self._run(server, config, lambda c: c.list_tools(),
                               timeout=resolve_call_timeout(config))

    async def call_tool(
        self, server: str, tool: str, args: dict, config: dict, *, progress_cb: Any = None,
    ) -> dict:
        """Call ``tool`` and return the RAW client result (``{content, isError, structuredContent}``).
        Result shaping (media blocks / offload marker) is the caller's post-processing. Raises
        :class:`MCPFault` on any contained fault."""
        timeout = resolve_call_timeout(config)
        return await self._run(
            server, config,
            lambda c: c.call_tool(tool, args or {}, progress_callback=progress_cb,
                                  timeout_seconds=timeout),
            timeout=timeout,
        )

    async def list_resources(self, server: str, config: dict) -> list[dict]:
        """Return the server's advertised resources. Raises :class:`MCPFault` on
        any contained fault (mirrors :meth:`list_tools`)."""
        return await self._run(server, config, lambda c: c.list_resources(),
                               timeout=resolve_call_timeout(config))

    async def list_resource_templates(self, server: str, config: dict) -> list[dict]:
        """Return the server's advertised resource templates. Raises
        :class:`MCPFault` on any contained fault (mirrors :meth:`list_tools`)."""
        return await self._run(server, config, lambda c: c.list_resource_templates(),
                               timeout=resolve_call_timeout(config))

    async def read_resource(self, server: str, uri: str, config: dict) -> dict:
        """Read one resource by URI and return ``{"contents": [...]}``. Raises
        :class:`MCPFault` on any contained fault (mirrors :meth:`call_tool`)."""
        return await self._run(server, config, lambda c: c.read_resource(uri),
                               timeout=resolve_call_timeout(config))

    async def subscribe_resource(self, server: str, uri: str, config: dict) -> None:
        """Subscribe to server-pushed ``resources/updated`` for ``uri``. Raises
        :class:`MCPFault` on any contained fault (mirrors :meth:`read_resource`).

        #2597 slice ②b: the injected pool MUST be a
        :class:`~reyn.mcp.connection_service.MCPConnectionService` (a HELD
        connection) for a subscription to be meaningful — see
        ``session.py``'s ``_mcp_subscribe_resource`` docstring, which refuses
        the call before it ever reaches here for an ephemeral (one-shot-pool)
        session. This method itself doesn't re-check that (the gateway is a
        thin dispatch seam, not a policy seam) — it just runs the op against
        whatever pool the caller injected.
        """
        return await self._run(server, config, lambda c: c.subscribe_resource(uri),
                               timeout=resolve_call_timeout(config))

    async def unsubscribe_resource(self, server: str, uri: str, config: dict) -> None:
        """Unsubscribe from ``uri``. Raises :class:`MCPFault` on any contained
        fault (mirrors :meth:`subscribe_resource`)."""
        return await self._run(server, config, lambda c: c.unsubscribe_resource(uri),
                               timeout=resolve_call_timeout(config))

    async def probe(self, server: str, config: dict) -> None:
        """Open the server (MCP initialize handshake) to verify reachability, then release it.
        Returns None on success; raises :class:`MCPFault` if the server cannot be reached/opened."""
        await self._run(server, config, lambda c: c.list_tools(),
                        timeout=resolve_call_timeout(config))
