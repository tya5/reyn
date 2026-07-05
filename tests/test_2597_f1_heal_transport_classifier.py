"""Tests for #2597 F1 — ``_HeldConnection._heal`` reconnect-classifier fix.

Real instances only, per the testing policy: no ``unittest.mock`` / ``MagicMock`` /
``AsyncMock`` / ``patch``. All three tests drive a REAL ``MCPConnectionService``
(the held-connection path) against REAL stdio MCP server subprocesses, reusing the
PID-survival idiom already established in ``test_2597_s2a_mcp_connection_service.py``
(the ``die`` test) / ``test_2597_s2a_mcp_resources_consumption.py`` (the
``resource://pid`` read): a tool/resource that returns ``os.getpid()`` of the SERVER
subprocess proves whether the held connection was discarded+reopened (PID changes) or
left untouched (PID survives) — not just Python object identity, which would not
distinguish "the same MCPClient object, but internally reconnected" from "genuinely
never touched".

Before the F1 fix, ``_heal`` caught bare ``MCPError`` — since post-S1 EVERY
``MCPClient`` method wraps ALL exceptions into some ``MCPError`` subclass, a
capability-gate refusal or an application-level protocol error (neither of which mean
the connection is dead) would ALSO trigger a spurious discard+reopen of a perfectly
healthy stdio subprocess. These tests pin the fix: only genuine transport-death
(``MCPTransportError``) heals; gate refusals (``MCPCapabilityError``) and app-level
errors (plain ``MCPError``) propagate with the connection left alone.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from reyn.mcp.client import MCPCapabilityError, MCPError, MCPTransportError
from reyn.mcp.connection_service import MCPConnectionService

_SUPPORT_DIR = Path(__file__).parent / "_support"
_ECHO_SERVER = _SUPPORT_DIR / "mcp_fastmcp_echo_server.py"
_TOOLS_ONLY_PID_SERVER = _SUPPORT_DIR / "mcp_tools_only_pid_server.py"

_ECHO_CFG = {"type": "stdio", "command": sys.executable, "args": [str(_ECHO_SERVER)]}
_TOOLS_ONLY_PID_CFG = {
    "type": "stdio", "command": sys.executable, "args": [str(_TOOLS_ONLY_PID_SERVER)],
}


async def _pid_via_call_tool(client) -> str:
    result = await client.call_tool("pid", {})
    return result["content"][0]["text"]


@pytest.mark.asyncio
async def test_capability_gate_refusal_does_not_recycle_held_connection():
    """Tier 2: a capability-gate refusal (``MCPCapabilityError``, raised by
    ``require_capability`` BEFORE any request reaches the server) on a held
    connection propagates WITHOUT discarding the connection — the ``mcp_tools_only_
    pid_server.py`` fixture advertises ``tools`` but NOT ``resources``, so
    ``list_resources()`` refuses fast. The SAME subprocess (identical PID, proven via
    the real ``pid`` tool) serves the call immediately before and after."""
    service = MCPConnectionService()
    try:
        client = await service.get("srv", _TOOLS_ONLY_PID_CFG)
        pid_before = await _pid_via_call_tool(client)

        with pytest.raises(MCPCapabilityError):
            await client.list_resources()

        pid_after = await _pid_via_call_tool(client)
        assert pid_after == pid_before, (
            "a gate refusal must NOT recycle a healthy held connection"
        )
    finally:
        await service.aclose()


@pytest.mark.asyncio
async def test_app_level_protocol_error_does_not_recycle_held_connection():
    """Tier 2: an application-level protocol error — the server is alive and
    responded, just with an error (reading an unknown resource URI against the real
    echo server raises a plain ``MCPError`` whose ``McpError`` cause carries the
    JSON-RPC "Resource not found" code, NOT the transport-death "Connection closed"
    code) — on a held connection propagates WITHOUT discarding the connection. The
    SAME subprocess (identical PID) serves the call immediately before and after."""
    service = MCPConnectionService()
    try:
        client = await service.get("srv", _ECHO_CFG)
        pid_before = await _pid_via_call_tool(client)

        with pytest.raises(MCPError) as exc_info:
            await client.read_resource("resource://does_not_exist")
        assert not isinstance(exc_info.value, MCPTransportError), (
            "an app-level protocol error must classify as plain MCPError, "
            "not MCPTransportError — only genuine transport-death does"
        )

        pid_after = await _pid_via_call_tool(client)
        assert pid_after == pid_before, (
            "an app-level protocol error must NOT recycle a healthy held connection"
        )
    finally:
        await service.aclose()


@pytest.mark.asyncio
async def test_genuine_transport_death_recycles_held_connection():
    """Tier 2: genuine transport death (killing the server subprocess via the real
    ``die`` tool) DOES classify as ``MCPTransportError`` and DOES heal the held
    connection — preserves the S2a die-test intent (see
    ``test_2597_s2a_mcp_connection_service.py``), now pinning the EXACT exception
    type the F1 classifier must raise for this case."""
    service = MCPConnectionService()
    try:
        client = await service.get("srv", _ECHO_CFG)
        pid_before = await _pid_via_call_tool(client)

        with pytest.raises(MCPTransportError):
            await client.call_tool("die", {})

        # the connection healed: the NEXT call succeeds on a FRESH subprocess.
        pid_after = await _pid_via_call_tool(client)
        assert pid_after != pid_before, "transport death must heal to a fresh subprocess"
    finally:
        await service.aclose()
