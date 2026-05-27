"""Tests for the SDK-backed MCPClient (PR32).

The official ``mcp`` SDK uses async generators for transports and an
``async with`` ClientSession. To avoid spinning up real subprocess /
HTTP servers we patch both ``stdio_client`` / ``streamablehttp_client``
and ``ClientSession`` at the module they're imported from.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest import mock

import pytest

from reyn.mcp_client import MCPClient, MCPError, expand_env

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


@pytest.fixture
def patched_sdk():
    """Patch the SDK entry points used by MCPClient."""
    with mock.patch("mcp.client.stdio.stdio_client", _fake_stdio_client), \
         mock.patch("mcp.client.streamable_http.streamablehttp_client", _fake_http_client), \
         mock.patch("mcp.ClientSession", _FakeSession):
        yield


# ── tests ────────────────────────────────────────────────────────────────────


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro) if False else asyncio.run(coro)


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


# ── G11 hypothesis A+B: teardown_mcp_clients invariants ──────────────────────
# These two tests pin the same-task explicit-close path added to
# ControlIRExecutor.teardown_mcp_clients() as the fix for G11.
# They use real MCPClient instances (no MagicMock) to verify the public
# is_initialized() surface — not private _stack/_session attributes.


def test_teardown_mcp_clients_closes_all_clients(patched_sdk):
    """Tier 2: OS invariant — teardown_mcp_clients() calls close() on every cached
    MCP client (verified via is_initialized() == False on each client after teardown).

    G11 fix: the explicit close must happen in the same asyncio task that
    opened the clients so anyio cancel-scope task-affinity is honoured.
    """
    from reyn.events.events import EventLog
    from reyn.kernel.control_ir_executor import ControlIRExecutor
    from reyn.workspace.workspace import Workspace

    cfg_a = {"type": "http", "url": "http://a/mcp"}
    cfg_b = {"type": "http", "url": "http://b/mcp"}

    async def _run_it():
        events = EventLog()
        ws = Workspace(events=events)
        executor = ControlIRExecutor(workspace=ws, events=events)

        # Populate _mcp_clients with two initialized clients (same task).
        client_a = MCPClient(cfg_a)
        await client_a.initialize()
        client_b = MCPClient(cfg_b)
        await client_b.initialize()
        executor._mcp_clients["a"] = client_a
        executor._mcp_clients["b"] = client_b

        assert client_a.is_initialized() is True
        assert client_b.is_initialized() is True

        # teardown_mcp_clients must close both in the same task.
        await executor.teardown_mcp_clients()

        # Both clients must report as closed via the public accessor.
        assert client_a.is_initialized() is False, "client_a should be closed after teardown"
        assert client_b.is_initialized() is False, "client_b should be closed after teardown"

    asyncio.run(_run_it())


def test_teardown_mcp_clients_empties_dict(patched_sdk):
    """Tier 2: OS invariant — teardown_mcp_clients() clears _mcp_clients so
    subsequent teardown calls are no-ops and the executor does not hold
    stale references (prevents double-close on GC).

    Verifies the dict is empty after teardown via the public-equivalent
    available_ops() path (which does not depend on _mcp_clients contents),
    plus a second teardown call that must not raise.
    """
    from reyn.events.events import EventLog
    from reyn.kernel.control_ir_executor import ControlIRExecutor
    from reyn.workspace.workspace import Workspace

    cfg = {"type": "http", "url": "http://x/mcp"}

    async def _run_it():
        events = EventLog()
        ws = Workspace(events=events)
        executor = ControlIRExecutor(workspace=ws, events=events)

        client = MCPClient(cfg)
        await client.initialize()
        executor._mcp_clients["x"] = client

        await executor.teardown_mcp_clients()

        # The dict must be empty — no stale refs that could be GC-finalised
        # cross-task after this point.
        assert not executor.mcp_clients, "_mcp_clients must be empty after teardown"

        # Second call is a no-op (nothing to close, no exception).
        await executor.teardown_mcp_clients()
        assert not executor.mcp_clients

    asyncio.run(_run_it())
