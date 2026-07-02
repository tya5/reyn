"""Tier 2: MCP probe constructs MCPClient correctly (hot-reload: installed servers' tools show up).

``_probe_server_tools`` / ``_probe_status`` did ``async with MCPClient(server_name, cfg)``, but
MCPClient takes ONE positional ``config: dict`` (agent_id keyword-only) and has NO async-context-
manager protocol. The server NAME (a str) was passed as ``config`` → ValueError, swallowed by the
bare except → every probe returned an empty tool list / a bogus "error:" status (the hot-reload gap:
installed servers showed no tools). Fix: ``MCPClient(cfg)`` + ``list_tools()`` / ``initialize()`` +
``finally: close()`` in the same task. Injected fake mirrors the real MCPClient surface.
"""
from __future__ import annotations

import pytest

import reyn.mcp.client as mcp_client_mod
from reyn.interfaces.cli.commands.mcp import _probe_server_tools, _probe_status


class _FakeMCPClient:
    """Mirrors the real MCPClient: ONE positional ``config: dict``, keyword-only ``agent_id``, and
    NO async-context-manager protocol — so the old ``MCPClient(name, cfg)`` + ``async with`` fails
    exactly as the real client did (a str is not a dict / 2 positionals)."""

    last_config = None

    def __init__(self, config, *, agent_id=None) -> None:
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
    monkeypatch.setattr(mcp_client_mod, "MCPClient", _FakeMCPClient)

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
    monkeypatch.setattr(mcp_client_mod, "MCPClient", _FakeMCPClient)

    assert _probe_status("mysrv", {"type": "stdio", "command": "x"}) == "ready"
