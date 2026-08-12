"""Low-level real MCP stdio server that paginates ``tools/list`` across 2 pages (#2597 S1).

Standalone script — run as a subprocess, never imported. Exists to prove FastMCP's
``Client.list_tools()`` (which MCPClient.list_tools() now delegates to) follows
``nextCursor`` instead of silently truncating at page 1, unlike the pre-swap ``mcp``
SDK ``ClientSession.list_tools()`` call the old client made directly.

Serves 4 tools across 2 pages of 2 (cursor = the next tool's index as a string).

#4412 pin-bump PR: ``lowlevel.Server``'s ``@list_tools()`` decorator is gone
on mcp 2.0 (confirmed live) — the handler is now a plain function passed as
``Server(...)``'s ``on_list_tools`` constructor kwarg, receiving
``(ctx, params: PaginatedRequestParams | None)`` directly. This actually
SIMPLIFIES this file: 2.0's own handler signature already carries cursor
control, so the old raw ``app.request_handlers[...]`` override (needed on
1.x because the decorator sugar couldn't express pagination) is gone too —
one handler function does the whole job. ``Tool.inputSchema`` ->
``input_schema``, ``ListToolsResult.nextCursor`` -> ``next_cursor``.
"""
from __future__ import annotations

import asyncio

import mcp.types as types
from mcp.server import Server
from mcp.server.stdio import stdio_server

_ALL_TOOLS = [
    types.Tool(name=f"tool_{i}", description=f"tool number {i}", input_schema={"type": "object"})
    for i in range(4)
]
_PAGE_SIZE = 2


async def list_tools(ctx: "object", params: "object") -> "types.ListToolsResult":
    cursor = getattr(params, "cursor", None) if params is not None else None
    start = int(cursor) if cursor else 0
    page = _ALL_TOOLS[start : start + _PAGE_SIZE]
    next_cursor = str(start + _PAGE_SIZE) if start + _PAGE_SIZE < len(_ALL_TOOLS) else None
    return types.ListToolsResult(tools=page, next_cursor=next_cursor)


app = Server("reyn-test-paginated", on_list_tools=list_tools)


async def main() -> None:
    async with stdio_server() as (read, write):
        await app.run(read, write, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
