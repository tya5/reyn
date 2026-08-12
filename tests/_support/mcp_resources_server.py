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

#4368 (mcp 2.0 port, arc #4412): this is a standalone test-only script, not
production surface the ``_mcp_server_boundary`` seam is scoped to (owner
ruling via lead-coder, #4368: growing the seam's registration API for every
test-only handler kind isn't warranted). Written plain, in the CURRENTLY
INSTALLED pin's own decorator API + field-name vocabulary (1.x today); the
arc's pin-bump PR flips this file's decorators/field names in the same
mechanical pass as every other pin-vocabulary call site.
"""
from __future__ import annotations

import asyncio

import mcp.types as types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

app = Server("reyn-test-resources")

_URI = "resource://greeting"


@app.list_resources()
async def list_resources() -> list[types.Resource]:
    return [types.Resource(uri=_URI, name="greeting", mimeType="text/plain")]


@app.read_resource()
async def read_resource(uri) -> str:
    return "hello from a resource"


async def main() -> None:
    async with stdio_server() as (read, write):
        await app.run(read, write, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
