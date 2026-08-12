"""Low-level real MCP stdio server that advertises the ``prompts`` capability
(#2597 slice ②c — prompts consumption).

Standalone script — run as a subprocess, never imported. Uses the LOW-LEVEL
``mcp.server.lowlevel.Server`` (like ``mcp_resources_server.py`` /
``mcp_paginated_tools_server.py``) rather than ``FastMCP`` — verified
empirically (see ``mcp_resources_server.py``'s module docstring) that a
FastMCP-built server ALWAYS advertises non-None ``tools``/``resources``/
``prompts``/``logging`` capabilities regardless of what it registers, so it
cannot demonstrate a server that does NOT advertise a capability. The
low-level SDK ``Server`` instead derives ``ServerCapabilities`` from which
handler types were actually registered (``get_capabilities()``), so a server
that registers ONLY ``@app.list_prompts()``/``@app.get_prompt()`` (no
``@app.list_tools()``) gets ``prompts`` non-None and ``tools`` None — the
real differentiator this gate slice needs to prove ``MCPClient.supports()``
reads the ACTUAL negotiated capabilities rather than a hardcoded reyn-side
assumption.
"""
from __future__ import annotations

import asyncio

import mcp.types as types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

_NAME = "greeting"
_DESCRIPTION = "A simple greeting prompt"
_RENDERED_TEXT = "hello from a prompt"


# #4368 (mcp 2.0 port): lowlevel.Server's @list_prompts()/@get_prompt()
# decorators are gone on mcp 2.0 (measured live) -- handlers are now plain
# functions passed as Server(...) constructor kwargs instead, so they are
# defined BEFORE construction. Each handler now receives
# (ctx: ServerRequestContext, params: <X>RequestParams) directly and must
# return the typed <X>Result object -- see src/reyn/mcp/server.py's own
# port commit for the full rationale, identical here. Prompt/PromptArgument/
# GetPromptRequestParams field names are unchanged between 1.x and 2.0
# (verified live) -- only the registration shape changed.
async def list_prompts(ctx: "object", params: "object") -> "types.ListPromptsResult":
    return types.ListPromptsResult(prompts=[
        types.Prompt(
            name=_NAME,
            description=_DESCRIPTION,
            arguments=[types.PromptArgument(name="style", description="tone", required=False)],
        )
    ])


async def get_prompt(
    ctx: "object", params: "types.GetPromptRequestParams",
) -> "types.GetPromptResult":
    return types.GetPromptResult(
        description=_DESCRIPTION,
        messages=[
            types.PromptMessage(
                role="user",
                content=types.TextContent(type="text", text=_RENDERED_TEXT),
            )
        ],
    )


app = Server(
    "reyn-test-prompts", on_list_prompts=list_prompts, on_get_prompt=get_prompt,
)


async def main() -> None:
    async with stdio_server() as (read, write):
        await app.run(read, write, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
