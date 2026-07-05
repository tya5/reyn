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

app = Server("reyn-test-tools-only-pid")

_PID_TOOL = types.Tool(
    name="pid", description="Return this server subprocess's PID.",
    inputSchema={"type": "object"},
)


@app.list_tools()
async def list_tools() -> list[types.Tool]:
    return [_PID_TOOL]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    if name != "pid":
        raise ValueError(f"Unknown tool: {name!r}")
    return [types.TextContent(type="text", text=str(os.getpid()))]


async def main() -> None:
    async with stdio_server() as (read, write):
        await app.run(read, write, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
