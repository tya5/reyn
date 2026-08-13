"""Real MCP stdio server that advertises ``resources`` WITHOUT the
``subscribe`` sub-capability, under EITHER negotiated era (#3698 review).

Standalone script — run as a subprocess, never imported.

Why this is a NEW low-level server, not the existing
``mcp_fastmcp_echo_server.py`` double: that double is built with the
high-level ``MCPServer`` class, which (measured live, #4368-era SDK)
registers ``"subscriptions/listen"`` in its request handlers
UNCONDITIONALLY — a bare ``MCPServer("x")``, no ``subscriptions=`` kwarg at
all, already has it (``"subscriptions/listen" in mcp._lowlevel_server.
_request_handlers`` is ``True``). Per the base SDK's own
``Server.get_capabilities`` (read directly): under a modern (2026-07-28+)
negotiation, ``resources.subscribe`` is derived ENTIRELY from whether
``"subscriptions/listen"`` is served — server-wide, regardless of whether
any registered resource actually honors subscriptions. So **no
``MCPServer``-built double can ever advertise ``resources.subscribe=False``
once a client negotiates modern era** — a real, structural SDK behavior
this file's own PRE-listen-support state relied on without realizing it
(#3698 PR-2 review: ``mcp_fastmcp_echo_server.py`` gained
``on_subscriptions_listen`` for an UNRELATED reason — the tools/prompts
list_changed dual-publish fix — which incidentally, and correctly per the
SDK's own semantics, flipped its ``resources.subscribe`` to ``True`` too,
breaking the 3 capability-gate tests that depended on it reading
``False``).

The low-level ``mcp.server.lowlevel.Server`` class, by contrast, has NO
default ``subscriptions/listen`` wiring at all (see
``mcp_subscribable_resources_server.py``'s own docstring) — omitting
``on_subscriptions_listen`` here (this file's only difference from that
one) keeps ``listen_served`` (and therefore ``resources.subscribe``)
``False`` under BOTH eras, which is exactly what
``MCPClient._require_resources_subscribe_capability``'s fail-fast gate
needs a real server to prove against.

Exposes:
  - resource ``resource://pid`` — content is this server process's PID (no
    subscribe/unsubscribe handler registered at all — a caller asking to
    subscribe/unsubscribe must get MCPCapabilityError, never reach the
    wire).
  - tool ``pid()`` — trivial liveness/no-op tool, unused by the
    capability-gate tests themselves but keeps ``tools/list`` non-empty
    (mirrors every other double in this directory).
"""
from __future__ import annotations

import asyncio
import os

import mcp.types as types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

_URI = "resource://pid"


async def list_resources(ctx: "object", params: "object") -> "types.ListResourcesResult":
    return types.ListResourcesResult(resources=[
        types.Resource(uri=_URI, name="pid", mime_type="text/plain"),
    ])


async def read_resource(
    ctx: "object", params: "types.ReadResourceRequestParams",
) -> "types.ReadResourceResult":
    return types.ReadResourceResult(contents=[types.TextResourceContents(
        uri=str(params.uri), mime_type="text/plain", text=str(os.getpid()),
    )])


async def list_tools(ctx: "object", params: "object") -> "types.ListToolsResult":
    return types.ListToolsResult(tools=[
        types.Tool(
            name="pid",
            description="Return this server process's PID.",
            input_schema={"type": "object", "properties": {}},
        ),
    ])


async def call_tool(
    ctx: "object", params: "types.CallToolRequestParams",
) -> "types.CallToolResult":
    if params.name == "pid":
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=str(os.getpid()))],
        )
    raise ValueError(f"unknown tool {params.name!r}")


app = Server(
    "reyn-test-resources-no-subscribe",
    on_list_resources=list_resources,
    on_read_resource=read_resource,
    on_list_tools=list_tools,
    on_call_tool=call_tool,
    # Deliberately NO on_subscribe_resource / on_unsubscribe_resource (the
    # legacy-era gate) and NO on_subscriptions_listen (the modern-era one)
    # — see module docstring: this is what keeps resources.subscribe=False
    # under BOTH eras.
)


async def main() -> None:
    async with stdio_server() as (read, write):
        await app.run(read, write, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
