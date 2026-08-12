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

_ALL_TOOLS = [
    types.Tool(name=f"tool_{i}", description=f"tool number {i}", input_schema={"type": "object"})
    for i in range(4)
]
_PAGE_SIZE = 2


# #4368 (mcp 2.0 port): the 1.x line's @app.list_tools() decorator only
# supported returning a bare tool list -- page boundaries needed cursor
# control, so this file bypassed the decorator sugar and installed a raw
# handler directly via app.request_handlers[ListToolsRequest] = ... (an
# internal SDK dict, not a public API). On 2.0 that whole workaround is
# gone: on_list_tools is a single constructor kwarg that already receives
# (ctx, params: PaginatedRequestParams | None) directly -- one function
# does what the decorator + raw-handler-dict override used to do together,
# no internal-attribute reach-in needed.
async def list_tools(
    ctx: "object", params: "types.PaginatedRequestParams | None",
) -> "types.ListToolsResult":
    cursor = params.cursor if params is not None else None
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
