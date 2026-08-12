"""Low-level real MCP stdio server that paginates ``tools/list`` across 2 pages (#2597 S1).

Standalone script — run as a subprocess, never imported. Exists to prove FastMCP's
``Client.list_tools()`` (which MCPClient.list_tools() now delegates to) follows
``nextCursor`` instead of silently truncating at page 1, unlike the pre-swap ``mcp``
SDK ``ClientSession.list_tools()`` call the old client made directly.

Serves 4 tools across 2 pages of 2 (cursor = the next tool's index as a string).
"""
from __future__ import annotations

import asyncio

import mcp.types as types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

app = Server("reyn-test-paginated")

_ALL_TOOLS = [
    types.Tool(name=f"tool_{i}", description=f"tool number {i}", inputSchema={"type": "object"})
    for i in range(4)
]
_PAGE_SIZE = 2


@app.list_tools()
async def list_tools(request: types.PaginatedRequestParams | None = None) -> list[types.Tool]:
    # The low-level Server API decorator only supports returning the tool list; page
    # boundaries need the raw request handler for cursor control, so this override goes
    # through app.request_handlers directly below instead of the decorator sugar.
    return _ALL_TOOLS


async def _handle_list_tools_paginated(req):
    cursor = req.params.cursor if req.params is not None else None
    start = int(cursor) if cursor else 0
    page = _ALL_TOOLS[start : start + _PAGE_SIZE]
    next_cursor = str(start + _PAGE_SIZE) if start + _PAGE_SIZE < len(_ALL_TOOLS) else None
    result = types.ListToolsResult(tools=page, nextCursor=next_cursor)
    return types.ServerResult(result)


app.request_handlers[types.ListToolsRequest] = _handle_list_tools_paginated


async def main() -> None:
    async with stdio_server() as (read, write):
        await app.run(read, write, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
