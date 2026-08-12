"""Low-level real MCP stdio server that advertises the ``resources`` capability
(#2597 capability gate slice).

Standalone script — run as a subprocess, never imported. Uses the LOW-LEVEL
``mcp.server.lowlevel.Server`` (like ``mcp_paginated_tools_server.py``) rather
than ``FastMCP`` — verified empirically that a FastMCP-built server ALWAYS
advertises non-None ``tools``/``resources``/``prompts``/``logging``
capabilities regardless of what it registers (FastMCP itself implements all
four handler types for every server it builds), so it cannot demonstrate a
server that does NOT advertise a capability. The low-level SDK ``Server``
instead derives ``ServerCapabilities`` from which handler types were actually
registered (``get_capabilities()``), so a server that registers ONLY
``@app.list_resources()``/``@app.read_resource()`` (no ``@app.list_tools()``)
gets ``resources`` non-None and ``tools`` None — the real differentiator this
gate slice needs to prove ``MCPClient.supports()`` reads the ACTUAL negotiated
capabilities rather than a hardcoded reyn-side assumption.
"""
from __future__ import annotations

import asyncio

import mcp.types as types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

_URI = "resource://greeting"


# #4368 (mcp 2.0 port): lowlevel.Server's @list_resources()/@read_resource()
# decorators are gone on mcp 2.0 (measured live) -- handlers are now plain
# functions passed as Server(...) constructor kwargs instead, so they are
# defined BEFORE construction. Each handler now receives
# (ctx: ServerRequestContext, params: <X>RequestParams) directly and must
# return the typed <X>Result object -- see src/reyn/mcp/server.py's own
# port commit for the full rationale, identical here. Resource.mimeType
# renamed to mime_type (verified live); on_read_resource must return a
# ReadResourceResult directly -- the old decorator's "bare str auto-wraps"
# convenience sugar is gone (confirmed: no normalisation layer between the
# handler's return value and the wire response on the 2.0 registration
# path).
async def list_resources(ctx: "object", params: "object") -> "types.ListResourcesResult":
    return types.ListResourcesResult(resources=[
        types.Resource(uri=_URI, name="greeting", mime_type="text/plain"),
    ])


async def read_resource(
    ctx: "object", params: "types.ReadResourceRequestParams",
) -> "types.ReadResourceResult":
    return types.ReadResourceResult(contents=[types.TextResourceContents(
        uri=str(params.uri), mime_type="text/plain", text="hello from a resource",
    )])


app = Server(
    "reyn-test-resources",
    on_list_resources=list_resources, on_read_resource=read_resource,
)


async def main() -> None:
    async with stdio_server() as (read, write):
        await app.run(read, write, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
