"""Real MCP stdio server that supports ``resources/subscribe`` (#2597 slice ②b).

Standalone script — run as a subprocess, never imported.

Why this is a NEW low-level server, not the existing FastMCP-built
``mcp_fastmcp_echo_server.py`` / ``mcp_resources_server.py`` doubles: the base
``mcp`` SDK's ``mcp.server.lowlevel.server.Server.get_capabilities`` HARD-CODES
``ResourcesCapability(subscribe=False, ...)`` whenever a ``ListResourcesRequest``
handler is registered — regardless of whether ``SubscribeRequest``/
``UnsubscribeRequest`` handlers are ALSO registered (verified by reading the
installed mcp SDK source: ``resources_capability = types.ResourcesCapability(
subscribe=False, listChanged=notification_options.resources_changed)`` — no
branch anywhere sets ``subscribe=True``). FastMCP's own low-level subclass
(``fastmcp.server.low_level.LowLevelServer.get_capabilities``) only patches
``capabilities.tasks``/``capabilities.extensions`` on top of the base result —
it never touches ``resources.subscribe`` either. Net effect: **no server built
with FastMCP's high-level ``FastMCP()`` class can ever advertise
``resources.subscribe=True``**, and slice ②b needs a REAL server that does (to
prove ``MCPClient.subscribe_resource`` on a server that DOES support it, not
just the already-covered fail-fast path against a server that doesn't).

Historically (mcp 1.x) this module subclassed the LOW-LEVEL
``mcp.server.lowlevel.Server`` directly and overrode ``get_capabilities`` to
flip ``resources.subscribe`` to True whenever a ``SubscribeRequest`` handler
was registered — the 1.x base implementation never set that field on its
own. #4368 (mcp 2.0 port): confirmed live that 2.0's OWN
``get_capabilities`` now derives ``resources.subscribe`` from whether a
``"resources/subscribe"`` handler is registered (the same "derive
capabilities from registered handlers" contract this file's subclass used
to have to patch in by hand) — the subclass override is gone, a plain
``Server(...)`` with ``on_subscribe_resource`` set is enough. This is a
real MCP server object, not a mock.

Exposes:
  - resource ``resource://counter`` — content is the current counter value.
  - ``@app.subscribe_resource()`` / ``@app.unsubscribe_resource()`` — no-op
    handlers (accepting the subscription is all a real server needs to do;
    the interesting behaviour is the PUSH below).
  - tool ``bump_and_notify()`` — increments the counter and pushes a REAL
    ``notifications/resources/updated`` for ``resource://counter`` via
    ``app.request_context.session.send_resource_updated(...)`` (the same raw
    ``ServerSession`` API a real MCP server implementer would call).
  - tool ``die()`` — kills the subprocess (transport-death simulation, mirrors
    ``mcp_fastmcp_echo_server.py``'s ``die`` tool) so reconnect/re-subscribe
    tests can simulate a genuine transport drop.
"""
from __future__ import annotations

import asyncio
import os

import mcp.types as types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

_URI = "resource://counter"

_counter = {"value": 0}


# #4368 (mcp 2.0 port): all 6 decorators this file used
# (@list_resources/@read_resource/@subscribe_resource/@unsubscribe_resource/
# @list_tools/@call_tool) are gone on mcp 2.0 (measured live) -- handlers
# are now plain functions passed as Server(...) constructor kwargs instead,
# so they are defined BEFORE construction. Each now receives
# (ctx: ServerRequestContext, params: <X>RequestParams) directly (the tool
# handler's ctx replaces the old ``app.request_context`` lookup for
# ``bump_and_notify``'s ``session.send_resource_updated`` call) and must
# return the typed <X>Result object -- see src/reyn/mcp/server.py's own
# port commit for the full rationale, identical here.
async def list_resources(ctx: "object", params: "object") -> "types.ListResourcesResult":
    return types.ListResourcesResult(resources=[
        types.Resource(uri=_URI, name="counter", mime_type="text/plain"),
    ])


async def read_resource(
    ctx: "object", params: "types.ReadResourceRequestParams",
) -> "types.ReadResourceResult":
    return types.ReadResourceResult(contents=[types.TextResourceContents(
        uri=str(params.uri), mime_type="text/plain", text=str(_counter["value"]),
    )])


async def subscribe_resource(
    ctx: "object", params: "types.SubscribeRequestParams",
) -> "types.EmptyResult":
    return types.EmptyResult()


async def unsubscribe_resource(
    ctx: "object", params: "types.UnsubscribeRequestParams",
) -> "types.EmptyResult":
    return types.EmptyResult()


async def list_tools(ctx: "object", params: "object") -> "types.ListToolsResult":
    return types.ListToolsResult(tools=[
        types.Tool(
            name="bump_and_notify",
            description="Increment the counter resource and push a real "
            "notifications/resources/updated for resource://counter.",
            input_schema={"type": "object", "properties": {}},
        ),
        types.Tool(
            name="die",
            description="Kill the subprocess (transport-death simulation).",
            input_schema={"type": "object", "properties": {}},
        ),
    ])


async def call_tool(
    ctx: "object", params: "types.CallToolRequestParams",
) -> "types.CallToolResult":
    name = params.name
    if name == "bump_and_notify":
        _counter["value"] += 1
        await ctx.session.send_resource_updated(_URI)
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=str(_counter["value"]))],
        )
    if name == "die":
        os._exit(1)
    raise ValueError(f"unknown tool {name!r}")


app = Server(
    "reyn-test-subscribable-resources",
    on_list_resources=list_resources,
    on_read_resource=read_resource,
    on_subscribe_resource=subscribe_resource,
    on_unsubscribe_resource=unsubscribe_resource,
    on_list_tools=list_tools,
    on_call_tool=call_tool,
)


async def main() -> None:
    async with stdio_server() as (read, write):
        await app.run(read, write, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
