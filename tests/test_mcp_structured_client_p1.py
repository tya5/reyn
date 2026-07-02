"""Tier 2: #a359 P1 — MCPClient structured async-context lifecycle (real stdio subprocess).

Root cause (a359): MCPClient's deferred ``self._stack`` (``initialize()`` stashes it; a later
``close()`` — possibly in a different task — does ``stack.aclose()``) exits the SDK stdio_client /
ClientSession INTERNAL anyio task-group scopes cross-task → "cancel scope crossed task boundary"
(Windows: BrokenResource / BaseExceptionGroup during subprocess teardown).

Fix: MCPClient is an async context manager — ``async with MCPClient(cfg) as c:`` opens (__aenter__)
and closes (__aexit__) in ONE task/scope, restoring the SDK's intended structured usage. This test
is the UNIX acceptance: the structured lifecycle works end-to-end against a REAL stdio subprocess and
completes cleanly. The Windows crash manifestation is Proactor-specific (owner-env diagnostic = P3),
so Unix survival is NECESSARY, not sufficient — hence a live subprocess, not a fake (the fake was
structurally blind to the SDK-internal task group that actually crashes).
"""
from __future__ import annotations

import sys
import tempfile
import textwrap
from pathlib import Path

import pytest

_SERVER_SRC = textwrap.dedent(
    '''
    from mcp.server.fastmcp import FastMCP
    mcp = FastMCP("p1acceptance")

    @mcp.tool()
    def echo(text: str) -> str:
        return text

    if __name__ == "__main__":
        mcp.run(transport="stdio")
    '''
)


@pytest.fixture()
def stdio_cfg(tmp_path):
    server = tmp_path / "p1_server.py"
    server.write_text(_SERVER_SRC, encoding="utf-8")
    return {"type": "stdio", "command": sys.executable, "args": [str(server)]}


@pytest.mark.asyncio
async def test_async_with_lifecycle_lists_tools(stdio_cfg):
    """Tier 2: `async with MCPClient(cfg) as c` opens + lists tools + closes in one task/scope,
    against a real stdio subprocess. Structured lifecycle survives (Unix acceptance)."""
    from reyn.mcp.client import MCPClient

    async with MCPClient(stdio_cfg) as client:
        tools = await client.list_tools()
    names = {t.get("name") for t in tools if isinstance(t, dict)}
    assert "echo" in names, "the real server's tool is listed via the structured lifecycle"


@pytest.mark.asyncio
async def test_async_with_calls_tool_and_reopens_cleanly(stdio_cfg):
    """Tier 2: a tool call works within the scope, and a SECOND independent `async with` opens+closes
    cleanly afterward — i.e. the first block fully tore down (no lingering scope prevents re-open)."""
    from reyn.mcp.client import MCPClient

    async with MCPClient(stdio_cfg) as client:
        result = await client.call_tool("echo", {"text": "hi"})
    assert result is not None

    # a fresh structured block after the first fully closed → a clean subprocess re-spawn
    async with MCPClient(stdio_cfg) as client2:
        tools = await client2.list_tools()
    assert any(t.get("name") == "echo" for t in tools if isinstance(t, dict))


@pytest.mark.asyncio
async def test_server_death_is_contained_not_uncontained_crash():
    """Tier 2: fault isolation (owner req) — an MCP server that DIES on startup surfaces a contained
    error through the real probe (empty tool list), it does NOT crash the caller. (P1 covers the
    simple-site error path via ``except Exception``; the per-turn-pool exception boundary for
    teardown BaseExceptionGroups — the transport-signature faults — is P2.)"""
    from reyn.interfaces.cli.commands.mcp import _probe_server_tools

    dead_cfg = {"type": "stdio", "command": sys.executable, "args": ["-c", "import sys; sys.exit(1)"]}
    name, tools = await _probe_server_tools("dead", dead_cfg)
    assert (name, tools) == ("dead", []), "server death is contained → empty result, not a crash"
