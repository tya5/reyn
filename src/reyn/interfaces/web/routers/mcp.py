"""MCP-over-SSE router — expose Reyn agents on the FastAPI gateway.

This is the SSE-transport counterpart of the stdio MCP server in
:mod:`reyn.interfaces.cli.commands.mcp` (and its core in :mod:`reyn.mcp.server`).
Outer LLM clients (Claude Desktop, Cursor, …) that support the MCP
SSE transport can connect to ``GET /mcp/sse`` and post client→server
JSON-RPC messages to ``POST /mcp/messages``.

The two transports share the **same** backing server constructed by
``reyn.mcp.server.build_server`` — so ``list_agents`` /
``send_to_agent`` semantics, P7 invariants, and budget / permission
gating are identical across both. Only the wire transport differs.

Why expose this on the existing FastAPI app instead of a separate
process?

  * The browser UI, the design renderer, and external MCP clients all
    share a single ``AgentRegistry`` / ``BudgetTracker`` /
    ``PermissionResolver`` — no cross-process coordination needed.
  * ``reyn web --reload`` works for the dev loop; MCP clients
    auto-reconnect on disconnect, so code edits don't require host
    restarts (vs. stdio, which couples the server lifecycle to the
    spawning Claude Desktop process).
  * The asyncio task-starvation that bit the stdio path (anyio +
    stdio_server scheduling) doesn't apply here — we're inside plain
    FastAPI / uvicorn asyncio.

P7: this module contains no skill-specific strings. All tool wiring
flows through ``build_server``.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from starlette.routing import Mount

from reyn.interfaces.web.deps import get_registry
from reyn.mcp.server import DEFAULT_SEND_TIMEOUT_SECONDS, build_server

router = APIRouter(tags=["mcp"])


# ── shared SSE transport ─────────────────────────────────────────────────────
#
# A single SseServerTransport instance must serve *both* the GET /mcp/sse
# stream and the POST /mcp/messages endpoint, because it tracks per-session
# write-stream handles in an internal dict keyed by session_id. Splitting
# this across two instances would mean POSTs land on a transport that
# never saw the corresponding GET, and every client message would 404.
#
# Lazy construction: the SDK import lives inside the factory so
# environments without `[mcp]` installed can still import this module
# (e.g., during help-text generation).
_sse_transport = None


def _get_sse_transport():
    global _sse_transport
    if _sse_transport is None:
        from mcp.server.sse import SseServerTransport
        # Endpoint that clients POST to. Must match the route below.
        _sse_transport = SseServerTransport("/mcp/messages")
    return _sse_transport


# ── GET /mcp/sse — server→client event stream ──────────────────────────────


@router.get("/mcp/sse", include_in_schema=False)
async def handle_sse(request: Request, registry=Depends(get_registry)) -> Response:
    """SSE GET endpoint: opens a server→client event stream.

    The stream sends:
      * an initial ``endpoint`` event whose data is the URL the client
        should POST messages to (= ``/mcp/messages?session_id=…``).
      * a sequence of ``message`` events carrying the server's JSON-RPC
        responses for that session.

    The connect_sse context manager owns the response lifecycle — by
    the time it exits, the SSE stream has been drained and the
    underlying ASGI ``send`` has emitted the final close. Returning a
    Starlette ``Response()`` here is purely to satisfy FastAPI's
    expectation of a return value; it is not transmitted.
    """
    transport = _get_sse_transport()
    server = build_server(registry, timeout=DEFAULT_SEND_TIMEOUT_SECONDS)
    init_options = server.create_initialization_options()

    # Note: request._send is private but is the canonical way to
    # access the ASGI send callable from within a FastAPI handler;
    # the SDK examples (server_sse.py) use the same approach.
    async with transport.connect_sse(
        request.scope, request.receive, request._send,
    ) as (read_stream, write_stream):
        await server.run(read_stream, write_stream, init_options)

    return Response()


# ── /mcp/messages — client→server POST endpoint ──────────────────────────────
#
# SseServerTransport.handle_post_message is itself an ASGI app, so we
# mount it as a Starlette sub-app rather than wrapping it in a FastAPI
# route. This keeps the SDK in charge of body parsing, session
# validation, and 4xx error responses (missing/invalid session_id →
# 400; unknown session → 404). Wrapping it in a FastAPI route would
# force us to re-inject ASGI plumbing that the SDK already does
# correctly.


def get_mcp_message_mount() -> Mount:
    """Return a Starlette ``Mount`` exposing the POST message endpoint.

    The host app calls this once and includes the Mount in its routing
    table. Lazy so import-time `mcp` failures don't break unrelated
    routers.
    """
    transport = _get_sse_transport()
    return Mount("/mcp/messages", app=transport.handle_post_message)


__all__ = ["router", "get_mcp_message_mount"]
