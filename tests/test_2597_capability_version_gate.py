"""Tests for the #2597 capability/version gate slice — negotiated protocol version
+ server capabilities captured at connect, gated feature calls.

Real instances only, per the testing policy: no ``mock.patch`` / ``MagicMock``.
Round-trips spawn REAL MCP servers (stdio subprocess):

  - ``mcp_fastmcp_echo_server.py`` (FastMCP) — the happy-path ``call_tool``
    round-trip. NOT used for capability-absence assertions: verified empirically
    that a FastMCP-built server always advertises non-None
    tools/resources/prompts/logging regardless of what it registers (FastMCP
    itself implements all four handler types for every server), so it cannot
    demonstrate an UNADVERTISED capability.
  - ``mcp_paginated_tools_server.py`` (low-level ``mcp.server.lowlevel.Server``,
    tools-only) — the negotiated ``ServerCapabilities`` there are DERIVED from
    which handler types were actually registered, so ``resources``/``prompts``/
    ``logging`` are genuinely None: the real "server did not advertise X" case.
  - ``mcp_resources_server.py`` (same low-level SDK, registers ONLY
    ``list_resources``/``read_resource``) — ``resources`` non-None, ``tools``
    None: proves ``supports()`` reads the server's ACTUAL negotiated
    capabilities in both directions, not a hardcoded reyn-side assumption.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from reyn.mcp.client import MCPClient, MCPError, require_capability
from reyn.mcp.connection_service import MCPConnectionService

_SUPPORT_DIR = Path(__file__).parent / "_support"
_ECHO_SERVER = _SUPPORT_DIR / "mcp_fastmcp_echo_server.py"
_TOOLS_ONLY_SERVER = _SUPPORT_DIR / "mcp_paginated_tools_server.py"
_RESOURCES_SERVER = _SUPPORT_DIR / "mcp_resources_server.py"


def _stdio_cfg(script: Path) -> dict:
    return {
        "type": "stdio",
        "command": sys.executable,
        "args": [str(script)],
    }


def test_negotiated_version_and_tools_capability_on_tools_only_server() -> None:
    """Tier 1: framework boundary — a real tools-only (low-level SDK) server
    negotiates a dated protocol version string and advertises "tools" but not
    "resources"/"prompts"."""

    async def _run_it():
        async with MCPClient(_stdio_cfg(_TOOLS_ONLY_SERVER)) as client:
            return (
                client.negotiated_version,
                client.supports("tools"),
                client.supports("resources"),
                client.supports("prompts"),
                client.advertised_capabilities(),
            )

    version, supports_tools, supports_resources, supports_prompts, advertised = asyncio.run(_run_it())
    assert isinstance(version, str) and version  # a dated revision, e.g. "2025-11-25"
    assert supports_tools is True
    assert supports_resources is False
    assert supports_prompts is False
    assert advertised == ["tools"]


def test_resources_capability_advertised_when_server_registers_a_resource() -> None:
    """Tier 1: framework boundary — a real server that registers ONLY a resource
    handler (no tools) negotiates a non-None ``resources`` capability and a None
    ``tools`` one, proving ``supports()`` reads the server's actual declared
    capabilities in both directions (not a reyn-side guess)."""

    async def _run_it():
        async with MCPClient(_stdio_cfg(_RESOURCES_SERVER)) as client:
            return client.supports("resources"), client.supports("tools")

    supports_resources, supports_tools = asyncio.run(_run_it())
    assert supports_resources is True
    assert supports_tools is False


def test_call_tool_succeeds_when_tools_capability_advertised() -> None:
    """Tier 1: the gate does not block a legitimate tool call on a real FastMCP
    server that advertises "tools" (the common case must stay unaffected)."""

    async def _run_it():
        async with MCPClient(_stdio_cfg(_ECHO_SERVER)) as client:
            return await client.call_tool("echo", {"text": "gated-ok"})

    result = asyncio.run(_run_it())
    assert result["isError"] is False
    assert result["content"][0]["text"] == "gated-ok"


def test_call_tool_fails_fast_against_a_server_that_does_not_advertise_tools() -> None:
    """Tier 1: the gate blocks ``call_tool`` against a real server that never
    advertised "tools" — the enforcement seam (:func:`require_capability`,
    called from ``MCPClient.call_tool``) fires before the request reaches the
    server, raising a clear reyn ``MCPError`` instead of a raw protocol error."""

    async def _run_it():
        async with MCPClient(_stdio_cfg(_RESOURCES_SERVER), server_name="resources-srv") as client:
            await client.call_tool("anything", {})

    with pytest.raises(MCPError) as exc_info:
        asyncio.run(_run_it())
    message = str(exc_info.value)
    assert "tools" in message
    assert "resources-srv" in message


def test_require_capability_fails_fast_with_clear_error_when_not_advertised() -> None:
    """Tier 1: :func:`require_capability` raises a clear reyn ``MCPError`` — naming
    the ungated capability, the server, and the negotiated version — instead of
    letting a request reach the server. Uses the public accessors (``supports`` /
    ``negotiated_version``), never private state."""

    async def _run_it():
        async with MCPClient(_stdio_cfg(_TOOLS_ONLY_SERVER), server_name="tools-only-srv") as client:
            assert client.supports("resources") is False
            try:
                require_capability(client, "resources")
            except MCPError as exc:
                return str(exc), client.negotiated_version
            return None, client.negotiated_version

    message, version = asyncio.run(_run_it())
    assert message is not None
    assert "resources" in message
    assert "tools-only-srv" in message
    assert version is not None and version in message


def test_supports_rejects_unknown_capability_name() -> None:
    """Tier 1: ``supports`` fails fast on a typo'd / unsupported capability name
    rather than silently returning False forever."""

    async def _run_it():
        async with MCPClient(_stdio_cfg(_ECHO_SERVER)) as client:
            try:
                client.supports("subscribe")
            except ValueError as exc:
                return str(exc)
            return None

    message = asyncio.run(_run_it())
    assert message is not None
    assert "subscribe" in message


def test_supports_is_conservative_before_initialize() -> None:
    """Tier 1: a not-yet-initialized client advertises nothing (conservative
    False) and has no negotiated version — never "everything supported" by
    default."""
    client = MCPClient(_stdio_cfg(_ECHO_SERVER))
    assert client.negotiated_version is None
    assert client.supports("tools") is False
    assert client.advertised_capabilities() == []


@pytest.mark.asyncio
async def test_connection_service_emits_mcp_initialized_with_version_and_capabilities():
    """Tier 2: the held-connection service (#2597 S2a) — the live session's MCP
    entry point — emits an ``mcp_initialized`` event carrying the REAL negotiated
    version + advertised capabilities on first connect, via the same ``emit_sink``
    seam S2b's notifications bridge uses. Observability seam per the #2597
    capability slice."""
    recorded: list[tuple[str, dict]] = []

    def _emit_sink(event_type: str, **data) -> None:
        recorded.append((event_type, data))

    service = MCPConnectionService(emit_sink=_emit_sink)
    try:
        await service.get("srv", _stdio_cfg(_TOOLS_ONLY_SERVER))
    finally:
        await service.aclose()

    init_events = [d for et, d in recorded if et == "mcp_initialized"]
    (event,) = init_events  # exactly one mcp_initialized event fired for this connect
    assert event["server"] == "srv"
    assert isinstance(event["negotiated_version"], str) and event["negotiated_version"]
    assert event["capabilities"] == ["tools"]
