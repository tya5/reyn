"""MCPClientPool — per-turn structured owner of MCP clients (a359 P2).

The a359 root cause: a cached ``MCPClient`` opened (initialized) lazily in whatever task ran an op,
then closed by a separate teardown in a possibly-different task, exits the SDK stdio_client /
ClientSession internal anyio task-group scopes cross-task → "cancel scope crossed task boundary"
(Windows: BrokenResourceError / BaseExceptionGroup during subprocess teardown).

The pool restores structured concurrency WHILE preserving subprocess reuse:
- entered once per run/turn in the run-owning task (``async with pool:``);
- ``get(server, cfg)`` opens the client (``MCPClient.__aenter__`` = initialize) IN THE POOL'S TASK
  and caches it for the scope (reuse across ops), failing fast if called from a different task;
- ``__aexit__`` closes every cached client in the pool's (owning) task — same task that opened them.

Fault isolation (owner req): ``__aexit__`` contains teardown faults — including ``BaseExceptionGroup``
from the SDK's internal task group — so a broken subprocess teardown cannot crash the run. It never
swallows cancellation: a group containing ``CancelledError`` is re-raised.
"""
from __future__ import annotations

import asyncio
import logging

from reyn.mcp.client import MCPClient

logger = logging.getLogger(__name__)


def describe_fault(exc: BaseException, *, limit: int = 600) -> str:
    """Summarise a caught fault as ``Type: message`` for an LLM-facing error tool-result.

    Owner req (a359 P2 fault-isolation): when an MCP call fails, the LLM must receive the fault
    CONTENT (so it can retry / adapt / report) — not an empty/silent error. For a
    ``BaseExceptionGroup`` (the SDK's internal task group surfaces faults this way), the member
    exceptions are aggregated (type + message each) rather than dumping a raw traceback; the result
    is truncated to ``limit`` chars. Generic — no MCP-specific vocabulary (P7-safe)."""
    if isinstance(exc, BaseExceptionGroup):
        parts = [f"{type(sub).__name__}: {sub}" for sub in exc.exceptions]
        text = f"{type(exc).__name__}({exc.message}): " + " | ".join(parts)
    else:
        text = f"{type(exc).__name__}: {exc}"
    return text if len(text) <= limit else text[:limit] + " …[truncated]"


# Control-flow exceptions that fault-isolation must NEVER contain — they must keep propagating so a
# cancelled / Ctrl-C'd / exiting process actually unwinds and shuts down.
_CONTROL_FLOW: tuple[type[BaseException], ...] = (asyncio.CancelledError, KeyboardInterrupt, SystemExit)


def is_or_contains_control_flow(exc: BaseException) -> bool:
    """True if ``exc`` is (or a BaseExceptionGroup that contains) a control-flow exception —
    ``CancelledError`` / ``KeyboardInterrupt`` / ``SystemExit``.

    Fault-isolation contains MCP transport/response faults (Exception + their groups) but must NEVER
    swallow control flow: a cancelled run must keep unwinding, and Ctrl-C / process-exit must still
    shut the process down. The SDK's internal task group surfaces faults as a ``BaseExceptionGroup``
    that may MIX a real transport error with a control-flow exception; ``split`` detects the
    control-flow sub-group (nested groups included)."""
    if isinstance(exc, _CONTROL_FLOW):
        return True
    if isinstance(exc, BaseExceptionGroup):
        matched, _rest = exc.split(_CONTROL_FLOW)
        return matched is not None
    return False


class MCPClientPool:
    """Structured, task-affine cache of MCP clients for one run/turn.

    Usage (in the run-owning task)::

        async with MCPClientPool() as pool:
            client = await pool.get("srv", cfg, agent_id=...)
            await client.list_tools()
        # every cached client is closed here, in this task
    """

    def __init__(self) -> None:
        self._clients: dict[str, MCPClient] = {}
        self._owner_task: "asyncio.Task | None" = None

    async def __aenter__(self) -> "MCPClientPool":
        self._owner_task = asyncio.current_task()
        return self

    @property
    def owner_task(self) -> "asyncio.Task | None":
        """The task that owns this pool (None outside the scope)."""
        return self._owner_task

    async def get(self, server: str, config: dict, *, agent_id: str | None = None) -> MCPClient:
        """Return a cached client for ``server``, opening (and caching) it on first use IN THE
        POOL'S TASK. Fails fast if called from a different task than the pool owner — opening a
        client's SDK task-group scope off the owning task is the cross-task hazard this pool exists
        to prevent (it would crash at ``__aexit__`` teardown instead of here, loudly)."""
        if self._owner_task is not None and asyncio.current_task() is not self._owner_task:
            raise RuntimeError(
                "MCPClientPool.get called from a task other than the pool owner — opening an MCP "
                "client off the owning task would leak an anyio cancel-scope across tasks. Run MCP "
                "ops in the pool's owning task."
            )
        if server not in self._clients:
            client = MCPClient(config, agent_id=agent_id)
            await client.__aenter__()  # enter (initialize) in the pool's task
            self._clients[server] = client
        return self._clients[server]

    async def __aexit__(self, *exc_info) -> None:
        clients = list(self._clients.items())
        self._clients.clear()
        self._owner_task = None
        for name, client in clients:
            try:
                await client.__aexit__(None, None, None)  # close in the pool's (owning) task
            except BaseException as exc:  # noqa: BLE001 — fault isolation (control flow re-raised)
                if is_or_contains_control_flow(exc):
                    raise
                logger.warning("MCP client %s teardown fault contained: %r", name, exc)
