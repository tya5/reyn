"""Tier 3a: #2421 — the MCPGateway contains a REAL dying MCP subprocess (not a fake in-task raise).

The seam's contain-all boundary + [1] structured sub-task join are proven here against a REAL stdio
MCP server that initializes, then its subprocess DIES mid-operation (``os._exit`` from inside
``list_tools``). This exercises the SDK ``stdio_client`` task group's reader/writer tasks observing
the broken transport — the off-task-orphaning hypothesis (b) — end-to-end, not a synthetic raise.

Expected: ``gateway.list_tools`` raises ``MCPFault`` (contained), the test process SURVIVES (no
uncontained BaseExceptionGroup escaping to the loop). This is the "fake-blind" gap lead flagged: the
fake tests prove the boundary LOGIC; this proves it against the real substrate that produced the
crash. Real subprocess (no mock); skipped if the ``mcp`` SDK server API is unavailable.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("mcp.server", reason="mcp SDK server API required for the real-substrate test")

# A real low-level stdio MCP server that handshakes fine, then its subprocess DIES on list_tools.
_DYING_SERVER = '''\
import os, asyncio
from mcp.server import Server
from mcp.server.stdio import stdio_server

app = Server("dying-server")

@app.list_tools()
async def list_tools():
    os._exit(1)  # the subprocess dies mid-operation (external dep missing / crash analogue)

async def main():
    async with stdio_server() as (r, w):
        await app.run(r, w, app.create_initialization_options())

asyncio.run(main())
'''


@pytest.mark.asyncio
async def test_gateway_contains_real_dying_subprocess(tmp_path, monkeypatch):
    """Tier 3a: a real MCP server whose subprocess dies mid-``list_tools`` is CONTAINED by the gateway
    as an MCPFault — reyn survives, no uncontained BaseExceptionGroup escapes. Proves [1]+[2] against
    the real substrate (SDK stdio_client task group on a broken transport)."""
    from reyn.mcp.gateway import MCPFault, MCPGateway

    monkeypatch.chdir(tmp_path)
    server = tmp_path / "dying_server.py"
    server.write_text(_DYING_SERVER, encoding="utf-8")
    cfg = {"type": "stdio", "command": sys.executable, "args": [str(server)],
           "call_timeout_seconds": 20}

    with pytest.raises(MCPFault) as ei:
        await MCPGateway().list_tools("dying", cfg)
    assert str(ei.value), "the fault content is surfaced for the LLM (non-empty)"
    # reaching here at all is the assertion that matters: the process SURVIVED (the fault was
    # contained as MCPFault, not an uncontained group crashing the run/loop).
