"""In-process client/server MCP round trip for tests (#4412 pin-bump PR).

mcp 2.0 removed ``mcp.shared.memory.create_connected_server_and_client_session``
(confirmed live: only ``create_client_server_memory_streams`` remains on
2.0) -- the exact convenience helper #4368's own third-axis fix
(``tests/runtime/test_mcp_server_resources_adapter.py``,
``tests/gateway/sample_slack/test_outbound.py``) relied on to drive a
handler through the SDK's REAL request-dispatch loop rather than calling
``server.request_handlers[...]`` directly (the direct-call form raises
``LookupError`` on ``request_ctx``, a ContextVar only the real dispatch
loop sets). This module hand-rolls the same shape the removed helper
provided, using ``create_client_server_memory_streams`` (which DID
survive) plus a task group running ``server.run()`` -- the same
construction the removed helper's own 1.x source did internally, just
not packaged as a context manager reyn can still import.
"""
from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any


@asynccontextmanager
async def connected_server_and_client_session(
    server: Any,
) -> "AsyncGenerator[Any, None]":
    """Yield a real ``ClientSession`` connected to *server* over in-memory
    streams, with ``server.run()`` actually driving the request-dispatch
    loop in a background task -- the caller still calls
    ``await session.initialize()`` itself (mirrors the removed helper's own
    contract, which never auto-initialized either)."""
    import anyio
    from mcp import ClientSession
    from mcp.shared.memory import create_client_server_memory_streams

    async with create_client_server_memory_streams() as (client_streams, server_streams):
        client_read, client_write = client_streams
        server_read, server_write = server_streams

        async with anyio.create_task_group() as tg:

            async def _run_server() -> None:
                await server.run(
                    server_read, server_write, server.create_initialization_options(),
                )

            tg.start_soon(_run_server)
            async with ClientSession(client_read, client_write) as session:
                yield session
            tg.cancel_scope.cancel()
