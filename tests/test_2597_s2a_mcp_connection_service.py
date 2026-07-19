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
from tests._support.agent_session import make_session

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
async def test_dead_connection_heals_next_call_after_transport_error():
    """Tier 2: S2a reconnect-on-demand (heal-then-propagate) — killing the server
    subprocess (the ``die`` tool, a REAL transport death) raises the transport MCPError
    to the caller ONCE (call_tool is NOT auto-retried — at-most-once for side-effectful
    tools), but HEALS the connection so the NEXT call transparently succeeds on a fresh
    subprocess (new PID) instead of the server being permanently wedged."""
    service = MCPConnectionService()
    try:
        client = await service.get("srv", _CFG)
        pid_before = (await client.call_tool("pid", {}))["content"][0]["text"]

        # The failing call raises (propagated, not silently retried).
        with pytest.raises(MCPError):
            await client.call_tool("die", {})

        # ...but the connection was HEALED: the NEXT call succeeds on a fresh subprocess.
        result = await client.call_tool("echo", {"text": "still alive"})
        assert result["isError"] is False
        assert result["content"][0]["text"] == "still alive"

        pid_after = (await client.call_tool("pid", {}))["content"][0]["text"]
        assert pid_after != pid_before, "heal opened a FRESH subprocess"
    finally:
        await service.aclose()


@pytest.mark.asyncio
async def test_call_tool_executed_at_most_once_across_mid_call_drop(tmp_path: Path):
    """Tier 2: S2a at-most-once — a side-effectful call_tool whose connection drops
    AFTER the server executed the tool (the drop-after-execution window) is NOT
    re-executed by the reconnect. ``bump_then_die`` durably records ONE execution on
    disk then kills the subprocess; the handle heals-then-propagates (raises), and the
    file shows the side effect ran EXACTLY ONCE — not twice (which a naive retry-on-error
    would cause: once per subprocess). This is the correctness guarantee the pre-S2a
    per-call pool had and the reconnect must preserve."""
    marker = tmp_path / "side_effects"
    # The stdio server subprocess is Seatbelt-sandboxed to its cwd, so point cwd at
    # tmp_path to make the marker file writable by the sandboxed tool.
    cfg = {**_CFG, "cwd": str(tmp_path)}
    service = MCPConnectionService()
    try:
        client = await service.get("srv", cfg)
        with pytest.raises(MCPError):
            await client.call_tool("bump_then_die", {"path": str(marker)})
        # the tool ran once (one appended byte), NOT twice (a retried re-execution).
        assert marker.read_text() == "x", "side-effectful call executed exactly once"

        # the connection still healed — a subsequent healthy call works on the fresh sub.
        result = await client.call_tool("echo", {"text": "post"})
        assert result["content"][0]["text"] == "post"
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

    session = make_session(agent_name="s2a-ephemeral-test", mcp_servers={"srv": _CFG})
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

    session = make_session(agent_name="s2a-persistent-test", mcp_servers={"srv": _CFG})
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
    server (no mocks).

    #3036: ``spawn_session_recorded`` now refreshes the spawned session's MCP roster
    from the on-disk config cascade right after construction (closing the "spawned
    session's roster frozen at the registry's boot-time session_factory snapshot"
    gap) — so ``srv`` must be written to the IN-set ``.reyn/config/mcp.yaml`` under
    THIS registry's ``project_root`` (not merely passed via the factory's in-memory
    ``mcp_servers=`` kwarg), and the factory must thread ``registry=`` through so the
    spawned session resolves that same root (mirrors every real frontend factory).
    Otherwise the #3036 refresh would read an empty/foreign cascade and wipe the
    in-memory-only ``srv`` entry before this test ever calls it."""
    import yaml

    from reyn.runtime.registry import AgentRegistry
    from reyn.runtime.session import Session

    (tmp_path / "reyn.yaml").write_text("model: standard\n", encoding="utf-8")
    cfg_path = tmp_path / ".reyn" / "config" / "mcp.yaml"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(yaml.safe_dump({"mcp": {"servers": {"srv": _CFG}}}), encoding="utf-8")

    holder: dict = {}

    def _factory(profile, *, presentation_consumer=None, intervention_bridge=None) -> Session:
        return make_session(agent_name=profile.name, mcp_servers={"srv": _CFG}, registry=holder.get("reg"))

    registry = AgentRegistry(project_root=tmp_path, session_factory=_factory)
    holder["reg"] = registry
    registry.create("s2a-owner")
    sid = await registry.spawn_session_recorded("s2a-owner", mode="persistent", presentation_consumer=None, intervention_bridge=None)
    session = registry._peek_session("s2a-owner", sid)

    await session._mcp_call_tool("srv", "echo", {"text": "hi"})
    assert session.mcp_held_servers() == ["srv"]

    removed = await registry.remove_session("s2a-owner", sid)
    assert removed is True
    assert session.mcp_held_servers() == [], (
        "remove_session must close held MCP connections during teardown"
    )
