"""Tier 2: #4686 — the list_mcp_subscriptions LLM-facing read tool.

Uses a real ``ToolContext`` + real handler against a hand-rolled fake host
implementing only ``mcp_list_subscriptions`` — the same pattern
``test_router_call_mcp_tool_enum.py``'s own handler tests use (a real
``RouterHostAdapter`` needs a full ``Session`` to construct, so a minimal
fake stands in for the host boundary specifically; no ``unittest.mock``).
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from reyn.core.events.state_log import StateLog
from reyn.core.offload.canonical import list_mcp_subscriptions_to_canonical
from reyn.tools.mcp import LIST_MCP_SUBSCRIPTIONS, _handle_list_mcp_subscriptions
from reyn.tools.types import RouterCallerState, ToolContext
from tests._support.agent_session import make_session
from tests._support.paths import REPO_ROOT

_SUBSCRIBABLE_SERVER = REPO_ROOT / "tests" / "_support" / "mcp_subscribable_resources_server.py"


def _ctx(host) -> ToolContext:
    return ToolContext(
        caller_kind="router",
        events=None,
        permission_resolver=None,
        workspace=None,
        router_state=RouterCallerState(host=host),
    )


def test_handler_returns_the_hosts_subscriptions_under_the_subscriptions_key():
    """Tier 2: the handler is a thin forwarder — host.mcp_list_subscriptions()'s
    return value passes through under the ``subscriptions`` key, matching
    ``_handle_list_mcp_servers``'s own ``{"servers": result}`` shape."""
    class _FakeHost:
        async def mcp_list_subscriptions(self) -> list[dict]:
            return [
                {"server": "broker", "mode": "legacy", "uris": ["broker://inbox/x"], "unhonored": None},
            ]

    result = asyncio.run(_handle_list_mcp_subscriptions({}, _ctx(_FakeHost())))
    assert result == {
        "subscriptions": [
            {"server": "broker", "mode": "legacy", "uris": ["broker://inbox/x"], "unhonored": None},
        ],
    }


def test_handler_returns_empty_list_when_nothing_is_subscribed():
    """Tier 2: no subscriptions anywhere → {"subscriptions": []}, not an
    error and not a missing key — the accept-side counterpart to the
    populated case above."""
    class _FakeHost:
        async def mcp_list_subscriptions(self) -> list[dict]:
            return []

    result = asyncio.run(_handle_list_mcp_subscriptions({}, _ctx(_FakeHost())))
    assert result == {"subscriptions": []}


def test_tool_definition_takes_no_parameters():
    """Tier 2: no server arg — this tool reports across every held connection
    at once (per-connection entries, never one server at a time), mirroring
    list_mcp_servers's own no-args shape."""
    assert LIST_MCP_SUBSCRIPTIONS.parameters["required"] == []
    assert LIST_MCP_SUBSCRIPTIONS.parameters["properties"] == {}


def test_tool_is_read_only_and_router_allowed():
    """Tier 2: discovery-only — same gate class as list_mcp_servers (no
    permission gate, no op-kind; see control-ir.md's own "Discovery is NOT
    gated" section, extended to this tool)."""
    assert LIST_MCP_SUBSCRIPTIONS.gates.router == "allow"
    assert LIST_MCP_SUBSCRIPTIONS.purity == "read_only"


# ── end-to-end: RouterHostAdapter → MCPConnectionService (real Session) ────


def test_end_to_end_real_session_reports_a_real_subscription(tmp_path: Path) -> None:
    """Tier 2: real Session, real MCPConnectionService, real subprocess MCP
    server — RouterHostAdapter.mcp_list_subscriptions() (reached via
    session._router_host, the same object the LIST_MCP_SUBSCRIPTIONS
    handler calls through ctx.router_state.host in production) reports the
    subscription this test actually established, not hand-built data."""
    session = make_session(
        agent_name="alice",
        state_log=StateLog(tmp_path / "state.wal"),
        snapshot_path=tmp_path / "snap.json",
    )

    async def _run():
        cfg = {
            "type": "stdio", "command": sys.executable,
            "args": [str(_SUBSCRIBABLE_SERVER)],
        }
        try:
            client = await session._mcp_connection_service.get("srv", cfg)
            await client.subscribe_resource("resource://counter")
            return await session._router_host.mcp_list_subscriptions()
        finally:
            await session._mcp_connection_service.aclose()

    result = asyncio.run(_run())
    assert result == [
        {
            "server": "srv", "mode": "legacy",
            "uris": ["resource://counter"], "unhonored": None,
        },
    ]

    canonical = list_mcp_subscriptions_to_canonical({"subscriptions": result})
    assert "1 MCP connection with subscriptions" in canonical["text"]
    assert "srv" in canonical["text"]
