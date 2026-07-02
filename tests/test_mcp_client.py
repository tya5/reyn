"""Tests for the SDK-backed MCPClient (PR32).

The official ``mcp`` SDK uses async generators for transports and an
``async with`` ClientSession. To avoid spinning up real subprocess /
HTTP servers we patch both ``stdio_client`` / ``streamablehttp_client``
and ``ClientSession`` at the module they're imported from.
"""
from __future__ import annotations

import asyncio
import warnings
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest import mock

import pytest

from reyn.mcp.client import MCPClient, MCPError, expand_env

# ── fakes ────────────────────────────────────────────────────────────────────


class _FakeContent:
    """Mimics ``mcp.types.TextContent`` enough for ``_result_to_dict``."""

    def __init__(self, text: str) -> None:
        self.type = "text"
        self.text = text

    def model_dump(self) -> dict:
        return {"type": self.type, "text": self.text}


class _FakeCallResult:
    def __init__(self, text: str, is_error: bool = False) -> None:
        self.content = [_FakeContent(text)]
        self.isError = is_error
        self.structuredContent = None


class _FakeTool:
    def __init__(self, name: str) -> None:
        self.name = name

    def model_dump(self) -> dict:
        return {"name": self.name, "description": "fake"}


class _FakeListResult:
    def __init__(self, names: list[str]) -> None:
        self.tools = [_FakeTool(n) for n in names]


class _FakeSession:
    """Mimics ``mcp.ClientSession`` as an async context manager."""

    last_init_args: dict = {}
    last_call: dict = {}

    def __init__(self, read_stream, write_stream, *args, **kwargs) -> None:
        self._read = read_stream
        self._write = write_stream
        self.entered = False
        self.closed = False

    async def __aenter__(self):
        self.entered = True
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        self.closed = True

    async def initialize(self):
        return SimpleNamespace(serverInfo=SimpleNamespace(name="fake"))

    async def call_tool(self, name: str, arguments: dict):
        _FakeSession.last_call = {"name": name, "arguments": arguments}
        if name == "boom":
            raise RuntimeError("simulated tool failure")
        text = f"echo:{name}:{sorted((arguments or {}).items())}"
        return _FakeCallResult(text)

    async def list_tools(self):
        return _FakeListResult(["echo", "ping"])


@asynccontextmanager
async def _fake_stdio_client(params, errlog=None):
    """Stand-in for ``mcp.client.stdio.stdio_client``.

    ``errlog`` is accepted to match the SDK signature (= MCPClient
    passes a tempfile when configured for stderr capture); the stand-in
    doesn't write to it.
    """
    _FakeSession.last_init_args = {
        "transport": "stdio",
        "command": params.command,
        "args": list(params.args),
        "env": dict(params.env) if params.env else None,
        "errlog_provided": errlog is not None,
    }
    yield ("read_stream_obj", "write_stream_obj")


@asynccontextmanager
async def _fake_http_client(url, headers=None, timeout=30):
    """Stand-in for ``mcp.client.streamable_http.streamablehttp_client``."""
    _FakeSession.last_init_args = {
        "transport": "http",
        "url": url,
        "headers": dict(headers or {}),
        "timeout": timeout,
    }
    yield ("read_stream_obj", "write_stream_obj", lambda: None)


class _NoopBackend:
    """A non-seatbelt sandbox backend stand-in (#1344): the stdio wrap then
    no-ops (warns), so these transport-passthrough tests stay isolated from the
    sandbox-wrap layer (which has its own test_mcp_client_sandbox_wrap.py)."""

    name = "noop"

    def available(self) -> bool:
        return True


@pytest.fixture
def patched_sdk():
    """Patch the SDK entry points used by MCPClient.

    Also pins a noop sandbox backend so the #1344 stdio sandbox-wrap leaves the
    command untouched here — these tests verify transport config forwarding, not
    the sandbox wrap (that is tested separately).
    """
    with mock.patch("mcp.client.stdio.stdio_client", _fake_stdio_client), \
         mock.patch("mcp.client.streamable_http.streamablehttp_client", _fake_http_client), \
         mock.patch("mcp.ClientSession", _FakeSession), \
         mock.patch("reyn.security.sandbox.get_default_backend", lambda config=None: _NoopBackend()), \
         warnings.catch_warnings():
        # The noop backend makes the #1344 stdio wrap a no-op + warn — expected
        # here (these tests isolate transport forwarding, not the sandbox wrap).
        warnings.filterwarnings("ignore", message=".*runs UNSANDBOXED.*")
        yield


# ── tests ────────────────────────────────────────────────────────────────────


def test_http_transport_round_trip(patched_sdk):
    """Tier 1: framework boundary (intentional SDK patch) — verifies HTTP transport config
    is forwarded correctly to the mcp SDK and that call_tool returns a valid result."""
    cfg = {
        "type": "http",
        "url": "http://localhost:9999/mcp",
        "headers": {"Authorization": "Bearer abc"},
    }

    async def _run_it():
        client = MCPClient(cfg)
        await client.initialize()
        result = await client.call_tool("read", {"path": "x.txt"})
        await client.close()
        return result

    result = asyncio.run(_run_it())
    assert _FakeSession.last_init_args["transport"] == "http"
    assert _FakeSession.last_init_args["url"] == "http://localhost:9999/mcp"
    assert result["isError"] is False
    assert result["content"][0]["type"] == "text"
    assert "echo:read" in result["content"][0]["text"]


def test_stdio_transport_round_trip(patched_sdk):
    """Tier 1: framework boundary (intentional SDK patch) — verifies stdio transport config
    is forwarded correctly to the mcp SDK and that list_tools/call_tool return valid results."""
    cfg = {
        "type": "stdio",
        "command": "/usr/bin/echo",
        "args": ["hello"],
        "env": {"FOO": "bar"},
    }

    async def _run_it():
        client = MCPClient(cfg)
        await client.initialize()
        tools = await client.list_tools()
        result = await client.call_tool("ping", {"n": 1})
        await client.close()
        return tools, result

    tools, result = asyncio.run(_run_it())
    assert _FakeSession.last_init_args["transport"] == "stdio"
    assert _FakeSession.last_init_args["command"] == "/usr/bin/echo"
    assert _FakeSession.last_init_args["args"] == ["hello"]
    assert _FakeSession.last_init_args["env"] == {"FOO": "bar"}
    assert {"echo", "ping"} == {t["name"] for t in tools}
    assert result["isError"] is False
    assert "echo:ping" in result["content"][0]["text"]


def test_invalid_type_rejected():
    """Tier 1: MCPClient public contract — unsupported transport type raises ValueError at construction."""
    with pytest.raises(ValueError, match="Unsupported MCP server type"):
        MCPClient({"type": "ftp", "url": "ftp://nope"})


def test_missing_type_rejected():
    """Tier 1: MCPClient public contract — missing transport type raises ValueError at construction."""
    with pytest.raises(ValueError, match="Unsupported MCP server type"):
        MCPClient({"url": "http://x"})


def test_env_var_expansion(monkeypatch):
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


def test_env_var_expansion_stdio_env(monkeypatch, patched_sdk):
    """Tier 1: framework boundary (intentional SDK patch) — expand_env in a stdio env dict
    propagates expanded values into the mcp SDK's transport parameters."""
    monkeypatch.setenv("MY_TOKEN", "t0k")
    cfg = expand_env(
        {
            "type": "stdio",
            "command": "/bin/cat",
            "args": [],
            "env": {"API_TOKEN": "${MY_TOKEN}"},
        }
    )

    async def _run_it():
        client = MCPClient(cfg)
        await client.initialize()
        await client.close()

    asyncio.run(_run_it())
    assert _FakeSession.last_init_args["env"] == {"API_TOKEN": "t0k"}


def test_close_releases_resources(patched_sdk):
    """Tier 2: MCPClient lifecycle invariant — initialize sets is_initialized() True;
    close tears down the session (is_initialized() False) and is idempotent.

    Case A (is_initialized() True after init): public accessor, no private access.
    Case B (is_initialized() False, stack/session released after close): all three
      private state checks (_initialized, _stack, _session) collapse into a single
      is_initialized() query — the invariant is that the session is closed, not which
      internal field holds None.
    Case C (double-close is no-op): verified via lack of exception on second close().
    """
    cfg = {"type": "http", "url": "http://x/mcp"}

    async def _run_it():
        client = MCPClient(cfg)
        await client.initialize()
        assert client.is_initialized() is True  # 案B: public accessor replaces _initialized
        await client.close()
        assert client.is_initialized() is False  # 案B: replaces _initialized/_stack/_session checks
        # Calling close again is a no-op (no exception raised).
        await client.close()
        assert client.is_initialized() is False

    asyncio.run(_run_it())


def test_call_tool_propagates_errors_as_mcp_error(patched_sdk):
    """Tier 1: framework boundary (intentional SDK patch) — tool-level runtime errors are
    wrapped and surfaced as MCPError with a 'tools/call' message."""
    cfg = {"type": "http", "url": "http://x/mcp"}

    async def _run_it():
        client = MCPClient(cfg)
        with pytest.raises(MCPError, match="tools/call"):
            await client.call_tool("boom", {})
        await client.close()

    asyncio.run(_run_it())


def test_sse_not_implemented(patched_sdk):
    """Tier 1: MCPClient public contract — 'sse' transport is accepted at construction
    (it is in _SUPPORTED_TYPES) but raises MCPError on initialize() until implemented."""
    cfg = {"type": "sse", "url": "http://x/sse"}

    async def _run_it():
        client = MCPClient(cfg)
        with pytest.raises(MCPError):
            await client.initialize()

    asyncio.run(_run_it())


# ── a359 P2: MCPClientPool same-task close-all + reuse ───────────────────────
# The pool replaces ControlIRExecutor.teardown_mcp_clients(): its __aexit__ closes every client
# opened via get() in the pool's (run-owning) task. Real MCPClient (no MagicMock); verified via the
# public is_initialized() surface. (Fault-containment incl. BaseExceptionGroup is pinned in the P2
# acceptance test.)


def test_pool_closes_all_clients_on_scope_exit(patched_sdk):
    """Tier 2: MCPClientPool.__aexit__ closes every client opened via get() in the pool's owning
    task — the a359 P2 replacement for teardown_mcp_clients (same-task close, robust-by-construction)."""
    from reyn.mcp.pool import MCPClientPool

    cfg_a = {"type": "http", "url": "http://a/mcp"}
    cfg_b = {"type": "http", "url": "http://b/mcp"}

    async def _run_it():
        pool = MCPClientPool()
        async with pool:
            client_a = await pool.get("a", cfg_a)
            client_b = await pool.get("b", cfg_b)
            assert client_a.is_initialized() is True
            assert client_b.is_initialized() is True
        # after the scope exits, every client is closed (same task)
        assert client_a.is_initialized() is False, "client_a closed on scope exit"
        assert client_b.is_initialized() is False, "client_b closed on scope exit"

    asyncio.run(_run_it())


def test_pool_reuses_cached_client_within_scope(patched_sdk):
    """Tier 2: a 2nd get() for the same server reuses the cached client (subprocess reuse preserved,
    no re-spawn) — the whole reason the pool caches rather than opening per call."""
    from reyn.mcp.pool import MCPClientPool

    cfg = {"type": "http", "url": "http://x/mcp"}

    async def _run_it():
        async with MCPClientPool() as pool:
            c1 = await pool.get("x", cfg)
            c2 = await pool.get("x", cfg)
            assert c1 is c2, "same cached client reused within the scope"

    asyncio.run(_run_it())

    asyncio.run(_run_it())
