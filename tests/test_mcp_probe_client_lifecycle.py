"""Tier 2: MCP probe returns a server's real tools / ready status through the MCPGateway seam.

``_probe_server_tools`` / ``_probe_status`` now route through ``MCPGateway`` (→ ``MCPClientPool`` →
``MCPClient``) rather than constructing an ``MCPClient`` directly (#2421). This still verifies the
probe surfaces the server's actual tools (cleaned) and a ready status, with the cfg DICT flowing
through to the client (the hot-reload requirement). The fake is patched where the POOL constructs it
(``reyn.mcp.pool.MCPClient``); patching ``reyn.mcp.client.MCPClient`` would miss the pool's
module-level binding (and be import-order-flaky).
"""
from __future__ import annotations

import pytest

import reyn.mcp.pool as pool_mod
from reyn.interfaces.cli.commands.mcp import _probe_server_tools, _probe_status


class _FakeMCPClient:
    """Mirrors the real MCPClient: ONE positional ``config: dict``, keyword-only ``agent_id``, and
    NO async-context-manager protocol — so the old ``MCPClient(name, cfg)`` + ``async with`` fails
    exactly as the real client did (a str is not a dict / 2 positionals)."""

    last_config = None

    def __init__(self, config, *, agent_id=None, server_name=None) -> None:
        if not isinstance(config, dict):
            raise ValueError("MCP server config must be a dict")
        _FakeMCPClient.last_config = config
        self.closed = False

    async def initialize(self) -> None:
        pass

    async def __aenter__(self):
        # #a359 P1: mirror the real MCPClient's async-CM protocol (probe now uses `async with`).
        return self

    async def __aexit__(self, *exc_info) -> None:
        await self.close()

    async def list_tools(self):
        return [{"name": "tool_a"}, {"name": "tool_b"}, {"error": "skip"}, {"no_name": 1}]

    async def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_probe_server_tools_returns_real_tools(monkeypatch):
    """Tier 2: CORE — the probe returns the server's actual tools (cleaned), constructed with the
    cfg DICT (not the name string). RED on the pre-fix ``MCPClient(server_name, cfg)`` (a str as
    config → ValueError → swallowed → empty list)."""
    monkeypatch.setattr(pool_mod, "MCPClient", _FakeMCPClient)

    name, tools = await _probe_server_tools("mysrv", {"type": "stdio", "command": "x"})

    assert name == "mysrv"
    assert tools == [{"name": "tool_a"}, {"name": "tool_b"}], "real tools returned + cleaned"
    assert _FakeMCPClient.last_config == {"type": "stdio", "command": "x"}, (
        "constructed with the cfg DICT (config-first), not the server name"
    )


def test_probe_status_ready(monkeypatch):
    """Tier 2: _probe_status returns 'ready' via a real initialize() handshake, not a swallowed
    ValueError from the mis-constructed client. (Sync test — _probe_status manages its own loop via
    run_async.)"""
    monkeypatch.setattr(pool_mod, "MCPClient", _FakeMCPClient)

    assert _probe_status("mysrv", {"type": "stdio", "command": "x"}) == "ready"
