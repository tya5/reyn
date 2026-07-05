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

This module subclasses the LOW-LEVEL ``mcp.server.lowlevel.Server`` directly
(the same base class FastMCP itself subclasses in ``fastmcp/server/low_level.py``)
and overrides ``get_capabilities`` to flip ``resources.subscribe`` to True
whenever a ``SubscribeRequest`` handler is registered — extending the SDK's own
"derive capabilities from registered handlers" contract (already used for
tools/prompts/resources ``listChanged``) to the one field the SDK's base
implementation never sets. This is a real MCP server object, not a mock.

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


class _SubscribableServer(Server):
    def get_capabilities(self, notification_options, experimental_capabilities):
        capabilities = super().get_capabilities(notification_options, experimental_capabilities)
        if types.SubscribeRequest in self.request_handlers and capabilities.resources is not None:
            capabilities = capabilities.model_copy(
                update={
                    "resources": capabilities.resources.model_copy(update={"subscribe": True}),
                }
            )
        return capabilities


app = _SubscribableServer("reyn-test-subscribable-resources")

_counter = {"value": 0}


@app.list_resources()
async def list_resources() -> list[types.Resource]:
    return [types.Resource(uri=_URI, name="counter", mimeType="text/plain")]


@app.read_resource()
async def read_resource(uri) -> str:
    return str(_counter["value"])


@app.subscribe_resource()
async def subscribe_resource(uri) -> None:
    return None


@app.unsubscribe_resource()
async def unsubscribe_resource(uri) -> None:
    return None


@app.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="bump_and_notify",
            description="Increment the counter resource and push a real "
            "notifications/resources/updated for resource://counter.",
            inputSchema={"type": "object", "properties": {}},
        ),
        types.Tool(
            name="die",
            description="Kill the subprocess (transport-death simulation).",
            inputSchema={"type": "object", "properties": {}},
        ),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    if name == "bump_and_notify":
        _counter["value"] += 1
        session = app.request_context.session
        await session.send_resource_updated(types.AnyUrl(_URI))
        return [types.TextContent(type="text", text=str(_counter["value"]))]
    if name == "die":
        os._exit(1)
    raise ValueError(f"unknown tool {name!r}")


async def main() -> None:
    async with stdio_server() as (read, write):
        await app.run(read, write, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
