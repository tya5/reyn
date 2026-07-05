"""Tests for MCPConnectionService (#2597 S2a — held-open MCP connections, Option C).

Real instances only, per the testing policy: no ``mock.patch`` / ``MagicMock``. Stdio
round-trips spawn a REAL subprocess running ``tests/_support/mcp_fastmcp_echo_server.py``
(a real FastMCP server) — connection reuse is proven via the server's real ``pid()`` tool
(same subprocess PID across calls), not just Python object identity. The ``die`` tool
genuinely kills the subprocess mid-call to exercise the reconnect-on-demand path against a
REAL transport failure (proven not to flip ``is_initialized()``/``is_connected()`` —
see connection_service.py's module docstring).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from reyn.mcp.client import MCPError
from reyn.mcp.connection_service import MCPConnectionService
from reyn.mcp.pool import MCPClientPool

_SUPPORT_DIR = Path(__file__).parent / "_support"
_ECHO_SERVER = _SUPPORT_DIR / "mcp_fastmcp_echo_server.py"

_CFG = {"type": "stdio", "command": sys.executable, "args": [str(_ECHO_SERVER)]}


@pytest.mark.asyncio
async def test_second_get_returns_same_held_client_no_rehandshake():
    """Tier 2: the CORE value of #2597 S2a — a 2nd ``get()`` for the same server returns
    the SAME handle (identity) AND hits the SAME subprocess (real ``pid()`` round-trip
    proves no re-handshake / no new subprocess), unlike the per-call pool it replaces."""
    service = MCPConnectionService()
    try:
        client_a = await service.get("srv", _CFG)
        client_b = await service.get("srv", _CFG)
        assert client_a is client_b, "same handle returned across get() calls"

        result_1 = await client_a.call_tool("pid", {})
        result_2 = await client_b.call_tool("pid", {})
        pid_1 = result_1["content"][0]["text"]
        pid_2 = result_2["content"][0]["text"]
        assert pid_1 == pid_2, "same underlying subprocess — no re-handshake"
    finally:
        await service.aclose()


@pytest.mark.asyncio
async def test_dead_connection_transparently_reconnects():
    """Tier 2: S2a reconnect-on-demand — killing the server subprocess (the ``die`` tool,
    a REAL transport death) does not permanently wedge the server: the next call on the
    SAME handle transparently reconnects (new subprocess PID) and succeeds, instead of
    failing forever."""
    service = MCPConnectionService()
    try:
        client = await service.get("srv", _CFG)
        pid_before = (await client.call_tool("pid", {}))["content"][0]["text"]

        with pytest.raises(MCPError):
            await client.call_tool("die", {})

        # transparent reconnect: the SAME handle keeps working post-death.
        result = await client.call_tool("echo", {"text": "still alive"})
        assert result["isError"] is False
        assert result["content"][0]["text"] == "still alive"

        pid_after = (await client.call_tool("pid", {}))["content"][0]["text"]
        assert pid_after != pid_before, "reconnect opened a FRESH subprocess"
    finally:
        await service.aclose()


@pytest.mark.asyncio
async def test_aclose_closes_all_held_clients():
    """Tier 2: ``aclose()`` tears down every held connection and is idempotent — the
    session-teardown contract (#2597 S2a scope item: no connections survive the service
    past teardown)."""
    service = MCPConnectionService()
    await service.get("srv", _CFG)
    assert service.held_servers() == ["srv"]

    await service.aclose()
    assert service.held_servers() == []

    # idempotent — a second aclose() on an already-empty service is a no-op, not an error.
    await service.aclose()
    assert service.held_servers() == []

    # a subsequent get() after aclose() opens a genuinely FRESH connection (lazy-connect
    # after teardown — proves aclose() actually released the subprocess, not just the
    # bookkeeping dict).
    client = await service.get("srv", _CFG)
    result = await client.call_tool("echo", {"text": "reopened"})
    assert result["content"][0]["text"] == "reopened"
    await service.aclose()


@pytest.mark.asyncio
async def test_call_result_parity_with_one_shot_pool():
    """Tier 2: parity — the connection service and the pre-#2597 one-shot
    ``MCPClientPool`` return byte-identical result shapes for the same call (the
    connection service is a drop-in ``pool=`` for MCPGateway, not a shape change)."""
    from reyn.mcp.gateway import MCPGateway

    service = MCPConnectionService()
    async with MCPClientPool() as pool:
        try:
            service_result = await MCPGateway(pool=service).call_tool(
                "srv", "echo", {"text": "parity"}, _CFG,
            )
            pool_result = await MCPGateway(pool=pool).call_tool(
                "srv", "echo", {"text": "parity"}, _CFG,
            )
        finally:
            await service.aclose()
    assert service_result == pool_result


@pytest.mark.asyncio
async def test_ephemeral_session_never_populates_connection_service():
    """Tier 2: F4 decision — an ephemeral session's MCP calls route through the one-shot
    pool, NOT the session-owned MCPConnectionService, so a sub-second-lived session never
    holds a connection open (would be pure churn). Real Session + real stdio server;
    asserts on the service's public ``held_servers()`` surface, not private state."""
    from reyn.runtime.session import Session

    session = Session(agent_name="s2a-ephemeral-test", mcp_servers={"srv": _CFG})
    session._ephemeral = True  # the registry sets this post-construction on an ephemeral spawn
    try:
        result = await session._mcp_call_tool("srv", "echo", {"text": "eph"})
        assert result.get("status") == "ok", result
        assert session.mcp_held_servers() == [], (
            "ephemeral session must not hold a connection open"
        )
    finally:
        await session.aclose_mcp_connections()


@pytest.mark.asyncio
async def test_non_ephemeral_session_holds_connection_across_calls():
    """Tier 2: the counterpart of the ephemeral test above — a non-ephemeral (persistent
    / main) session DOES hold the connection open across two ``_mcp_call_tool`` calls
    (the S2a value: no re-handshake on a 2nd tool call within the session)."""
    from reyn.runtime.session import Session

    session = Session(agent_name="s2a-persistent-test", mcp_servers={"srv": _CFG})
    try:
        r1 = await session._mcp_call_tool("srv", "pid", {})
        assert session.mcp_held_servers() == ["srv"]
        r2 = await session._mcp_call_tool("srv", "pid", {})
        assert r1["content"] == r2["content"], "same subprocess reused across calls"
    finally:
        await session.aclose_mcp_connections()
        assert session.mcp_held_servers() == []


@pytest.mark.asyncio
async def test_remove_session_teardown_closes_held_connections(tmp_path: Path):
    """Tier 2: registry.remove_session's teardown seam closes any MCP connections a
    spawned session held open (#2597 S2a wiring) — a dropped/rewound session must not
    leak an open subprocess. Real AgentRegistry + real spawned session + real stdio
    server (no mocks)."""
    from reyn.runtime.registry import AgentRegistry
    from reyn.runtime.session import Session

    def _factory(profile) -> Session:
        return Session(agent_name=profile.name, mcp_servers={"srv": _CFG})

    registry = AgentRegistry(project_root=tmp_path, session_factory=_factory)
    registry.create("s2a-owner")
    sid = await registry.spawn_session_recorded("s2a-owner", mode="persistent")
    session = registry._peek_session("s2a-owner", sid)

    await session._mcp_call_tool("srv", "echo", {"text": "hi"})
    assert session.mcp_held_servers() == ["srv"]

    removed = await registry.remove_session("s2a-owner", sid)
    assert removed is True
    assert session.mcp_held_servers() == [], (
        "remove_session must close held MCP connections during teardown"
    )
