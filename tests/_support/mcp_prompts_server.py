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

app = Server("reyn-test-prompts")

_NAME = "greeting"
_DESCRIPTION = "A simple greeting prompt"
_RENDERED_TEXT = "hello from a prompt"


@app.list_prompts()
async def list_prompts() -> list[types.Prompt]:
    return [
        types.Prompt(
            name=_NAME,
            description=_DESCRIPTION,
            arguments=[types.PromptArgument(name="style", description="tone", required=False)],
        )
    ]


@app.get_prompt()
async def get_prompt(name: str, arguments: dict | None) -> types.GetPromptResult:
    return types.GetPromptResult(
        description=_DESCRIPTION,
        messages=[
            types.PromptMessage(
                role="user",
                content=types.TextContent(type="text", text=_RENDERED_TEXT),
            )
        ],
    )


async def main() -> None:
    async with stdio_server() as (read, write):
        await app.run(read, write, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
