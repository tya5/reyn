"""Low-level real MCP stdio server that advertises ONLY the ``tools`` capability,
with a ``pid`` tool (#2597 F1 — ``_heal`` reconnect-classifier tests).

Standalone script — run as a subprocess, never imported. Combines two properties
neither existing test-support fixture has both of:

  - Like ``mcp_paginated_tools_server.py``: uses the LOW-LEVEL ``mcp.server.
    lowlevel.Server`` (not FastMCP — verified empirically in that fixture's
    docstring that a FastMCP-built server always advertises ALL four capabilities
    regardless of what it registers, so it cannot demonstrate a server that
    genuinely does NOT advertise ``resources``). Registers ONLY
    ``@app.list_tools()``/``@app.call_tool()`` — no resource handlers — so its
    negotiated ``resources`` capability is None, the real "gate refuses" case
    F1's ``MCPCapabilityError`` test needs.
  - Like ``mcp_fastmcp_echo_server.py``'s ``pid()`` tool: exposes a ``pid`` tool
    returning ``os.getpid()`` of THIS server subprocess, so a held-connection
    test can prove the SAME subprocess survives a gate-refused / app-level-error
    call (no reconnect) by comparing PIDs before and after.
"""
from __future__ import annotations

import asyncio
import os

import mcp.types as types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

_PID_TOOL = types.Tool(
    name="pid", description="Return this server subprocess's PID.",
    input_schema={"type": "object"},
)


# #4368 (mcp 2.0 port): lowlevel.Server's @list_tools()/@call_tool()
# decorators are gone on mcp 2.0 (measured live) -- handlers are now plain
# functions passed as Server(...) constructor kwargs instead, so they are
# defined BEFORE construction. Each handler now receives
# (ctx: ServerRequestContext, params: <X>RequestParams) directly and must
# return the typed <X>Result object -- see src/reyn/mcp/server.py's own
# port commit for the full rationale, identical here.
async def list_tools(ctx: "object", params: "object") -> "types.ListToolsResult":
    return types.ListToolsResult(tools=[_PID_TOOL])


async def call_tool(
    ctx: "object", params: "types.CallToolRequestParams",
) -> "types.CallToolResult":
    if params.name != "pid":
        raise ValueError(f"Unknown tool: {params.name!r}")
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=str(os.getpid()))],
    )


app = Server(
    "reyn-test-tools-only-pid", on_list_tools=list_tools, on_call_tool=call_tool,
)


async def main() -> None:
    async with stdio_server() as (read, write):
        await app.run(read, write, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
