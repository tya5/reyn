"""Tests for the FastMCP-backed MCPClient (#2597 S1 — mcp SDK -> fastmcp swap).

Real instances only, per the testing policy: no ``mock.patch`` / ``MagicMock`` on
the transport or session. Stdio round-trips spawn a REAL subprocess running
``tests/_support/mcp_fastmcp_echo_server.py`` (a real FastMCP server); http
round-trips spin a REAL local uvicorn server via ``FastMCP.run_async`` on an
ephemeral port. Pagination is proven against a real low-level MCP server
(``tests/_support/mcp_paginated_tools_server.py``) that serves 2 pages.
"""
from __future__ import annotations

import asyncio
import socket
import sys
from pathlib import Path

import pytest

from reyn.mcp.client import MCPClient, MCPError, expand_env

_SUPPORT_DIR = Path(__file__).parent / "_support"
_ECHO_SERVER = _SUPPORT_DIR / "mcp_fastmcp_echo_server.py"
_PAGINATED_SERVER = _SUPPORT_DIR / "mcp_paginated_tools_server.py"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _HttpEchoServer:
    """Runs the real echo FastMCP server in-process via ``run_async`` on an
    ephemeral port, as a background asyncio task — no subprocess needed for
    the http-transport tests, but no mock either: a real bound socket serving
    the real MCP protocol."""

    def __init__(self) -> None:
        self.port = _free_port()
        self.url = f"http://127.0.0.1:{self.port}/mcp/"
        self._task: asyncio.Task | None = None

    async def __aenter__(self) -> "_HttpEchoServer":
        sys.path.insert(0, str(_SUPPORT_DIR))
        import mcp_fastmcp_echo_server as server_mod

        self._task = asyncio.create_task(
            server_mod.mcp.run_async(
                transport="http", host="127.0.0.1", port=self.port, show_banner=False,
            )
        )
        # Poll until the socket accepts connections instead of a fixed sleep.
        for _ in range(100):
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=0.1):
                    break
            except OSError:
                await asyncio.sleep(0.05)
        return self

    async def __aexit__(self, *exc_info) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 — best-effort teardown
                pass


# ── round-trip tests ─────────────────────────────────────────────────────────


def test_stdio_transport_round_trip() -> None:
    """Tier 1: framework boundary — a real stdio subprocess handshakes, lists tools, and
    executes a tool call through the FastMCP-backed transport."""
    cfg = {
        "type": "stdio",
        "command": sys.executable,
        "args": [str(_ECHO_SERVER)],
    }

    async def _run_it():
        async with MCPClient(cfg) as client:
            tools = await client.list_tools()
            result = await client.call_tool("echo", {"text": "hello"})
            return tools, result

    tools, result = asyncio.run(_run_it())
    names = {t["name"] for t in tools}
    assert {"echo", "boom", "show_headers", "progress"} <= names
    assert result["isError"] is False
    assert result["content"][0]["type"] == "text"
    assert result["content"][0]["text"] == "hello"


def test_http_transport_round_trip() -> None:
    """Tier 1: framework boundary — a real local HTTP MCP server (uvicorn via
    FastMCP.run_async) handshakes and executes a tool call over Streamable HTTP."""

    async def _run_it():
        async with _HttpEchoServer() as server:
            cfg = {
                "type": "http",
                "url": server.url,
                "headers": {"Authorization": "Bearer abc"},
            }
            async with MCPClient(cfg) as client:
                result = await client.call_tool("echo", {"text": "hi-http"})
                return result

    result = asyncio.run(_run_it())
    assert result["isError"] is False
    assert result["content"][0]["text"] == "hi-http"


def test_http_transport_forwards_agent_id_header() -> None:
    """Tier 1: FP-0016 Component E — ``X-Reyn-Agent-Id`` reaches the real server."""

    async def _run_it():
        async with _HttpEchoServer() as server:
            cfg = {"type": "http", "url": server.url}
            async with MCPClient(cfg, agent_id="reyn/test-agent") as client:
                result = await client.call_tool("show_headers", {})
                return result

    result = asyncio.run(_run_it())
    assert result["structuredContent"]["x-reyn-agent-id"] == "reyn/test-agent"


def test_list_tools_follows_pagination_cursor() -> None:
    """Tier 1: #2597 S1 free win — list_tools() follows nextCursor across pages instead
    of silently truncating at page 1 (the pre-swap bug). A real 2-page low-level server."""
    cfg = {"type": "stdio", "command": sys.executable, "args": [str(_PAGINATED_SERVER)]}

    async def _run_it():
        async with MCPClient(cfg) as client:
            return await client.list_tools()

    tools = asyncio.run(_run_it())
    names = {t["name"] for t in tools}
    assert names == {"tool_0", "tool_1", "tool_2", "tool_3"}, (
        "all 4 tools across both pages must be returned, not just page 1's 2"
    )


def test_invalid_type_rejected() -> None:
    """Tier 1: MCPClient public contract — unsupported transport type raises ValueError at construction."""
    with pytest.raises(ValueError, match="Unsupported MCP server type"):
        MCPClient({"type": "ftp", "url": "ftp://nope"})


def test_missing_type_rejected() -> None:
    """Tier 1: MCPClient public contract — missing transport type raises ValueError at construction."""
    with pytest.raises(ValueError, match="Unsupported MCP server type"):
        MCPClient({"url": "http://x"})


def test_env_var_expansion(monkeypatch) -> None:
    """Tier 1: expand_env public contract — ${VAR} tokens in string values are replaced
    with the corresponding environment variable."""
    monkeypatch.setenv("MY_TOKEN", "s3cret")
    monkeypatch.setenv("MY_HOST", "example.com")
    cfg = {
        "type": "http",
        "url": "https://${MY_HOST}/mcp",
        "headers": {"Authorization": "Bearer ${MY_TOKEN}"},
    }
    expanded = expand_env(cfg)
    assert expanded["url"] == "https://example.com/mcp"
    assert expanded["headers"]["Authorization"] == "Bearer s3cret"


def test_env_var_expansion_stdio_env(monkeypatch) -> None:
    """Tier 1: framework boundary — expand_env in a stdio env dict propagates expanded
    values into the real subprocess's environment (proven by the subprocess echoing it back)."""
    monkeypatch.setenv("MY_TOKEN", "t0k")
    cfg = expand_env(
        {
            "type": "stdio",
            "command": sys.executable,
            "args": [
                "-c",
                "import os,sys; sys.stdout.write(os.environ.get('API_TOKEN',''))",
            ],
            "env": {"API_TOKEN": "${MY_TOKEN}", **{"PATH": "/usr/bin:/bin"}},
        }
    )
    assert cfg["env"]["API_TOKEN"] == "t0k"
    # Direct transport-object assertion (real fastmcp.client.transports.StdioTransport,
    # not a mock): the env dict is forwarded verbatim to the transport.
    client = MCPClient(cfg)
    transport = client._open_transport()
    assert transport.env["API_TOKEN"] == "t0k"


def test_close_releases_resources() -> None:
    """Tier 2: MCPClient lifecycle invariant — initialize sets is_initialized() True;
    close tears down the session (is_initialized() False) and is idempotent."""
    cfg = {"type": "stdio", "command": sys.executable, "args": [str(_ECHO_SERVER)]}

    async def _run_it():
        client = MCPClient(cfg)
        await client.initialize()
        assert client.is_initialized() is True
        await client.close()
        assert client.is_initialized() is False
        # Calling close again is a no-op (no exception raised).
        await client.close()
        assert client.is_initialized() is False

    asyncio.run(_run_it())


def test_call_tool_propagates_tool_error_not_transport_crash() -> None:
    """Tier 1: framework boundary — a tool that raises server-side surfaces as
    ``isError: True`` in the result (MCP protocol-level tool error), not an MCPError —
    matching the pre-swap contract (``call_tool_mcp`` never raises on ``isError``)."""
    cfg = {"type": "stdio", "command": sys.executable, "args": [str(_ECHO_SERVER)]}

    async def _run_it():
        async with MCPClient(cfg) as client:
            return await client.call_tool("boom", {})

    result = asyncio.run(_run_it())
    assert result["isError"] is True
    assert "simulated tool failure" in result["content"][0]["text"]


def test_call_tool_propagates_transport_errors_as_mcp_error() -> None:
    """Tier 1: framework boundary — a genuine transport-level failure (the subprocess DIES
    mid-call, unlike ``boom``'s protocol-level ``isError`` result) is wrapped and surfaced
    as MCPError with a 'tools/call' message rather than a bare/uncontained exception."""
    cfg = {"type": "stdio", "command": sys.executable, "args": [str(_ECHO_SERVER)]}

    async def _run_it():
        async with MCPClient(cfg) as client:
            await client.call_tool("die", {})

    with pytest.raises(MCPError, match="tools/call"):
        asyncio.run(_run_it())


def test_sse_transport_round_trip() -> None:
    """Tier 1: #2597 S1 free win — SSE, previously an unconditional NotImplementedError,
    now round-trips against a real local SSE MCP server."""

    async def _run_it():
        port = _free_port()
        sys.path.insert(0, str(_SUPPORT_DIR))
        import mcp_fastmcp_echo_server as server_mod

        task = asyncio.create_task(
            server_mod.mcp.run_async(
                transport="sse", host="127.0.0.1", port=port, show_banner=False,
            )
        )
        try:
            for _ in range(100):
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                        break
                except OSError:
                    await asyncio.sleep(0.05)
            cfg = {"type": "sse", "url": f"http://127.0.0.1:{port}/sse/"}
            async with MCPClient(cfg) as client:
                return await client.call_tool("echo", {"text": "sse-hi"})
        finally:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    result = asyncio.run(_run_it())
    assert result["isError"] is False
    assert result["content"][0]["text"] == "sse-hi"


# ── a359 P2: MCPClientPool same-task close-all + reuse ───────────────────────
# The pool replaces ControlIRExecutor.teardown_mcp_clients(): its __aexit__ closes every
# client opened via get() in the pool's (run-owning) task. Real MCPClient against real
# subprocesses; verified via the public is_initialized() surface.


def test_pool_closes_all_clients_on_scope_exit() -> None:
    """Tier 2: MCPClientPool.__aexit__ closes every client opened via get() in the pool's owning
    task — the a359 P2 replacement for teardown_mcp_clients (same-task close, robust-by-construction)."""
    from reyn.mcp.pool import MCPClientPool

    cfg_a = {"type": "stdio", "command": sys.executable, "args": [str(_ECHO_SERVER)]}
    cfg_b = {"type": "stdio", "command": sys.executable, "args": ["-c", "1"]}

    async def _run_it():
        pool = MCPClientPool()
        async with pool:
            client_a = await pool.get("a", cfg_a)
            assert client_a.is_initialized() is True
        assert client_a.is_initialized() is False, "client_a closed on scope exit"

    asyncio.run(_run_it())


def test_pool_reuses_cached_client_within_scope() -> None:
    """Tier 2: a 2nd get() for the same server reuses the cached client (subprocess reuse preserved,
    no re-spawn) — the whole reason the pool caches rather than opening per call."""
    from reyn.mcp.pool import MCPClientPool

    cfg = {"type": "stdio", "command": sys.executable, "args": [str(_ECHO_SERVER)]}

    async def _run_it():
        async with MCPClientPool() as pool:
            c1 = await pool.get("x", cfg)
            c2 = await pool.get("x", cfg)
            assert c1 is c2, "same cached client reused within the scope"

    asyncio.run(_run_it())
