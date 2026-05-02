"""Tests for the SDK-backed MCPClient (PR32).

The official ``mcp`` SDK uses async generators for transports and an
``async with`` ClientSession. To avoid spinning up real subprocess /
HTTP servers we patch both ``stdio_client`` / ``streamablehttp_client``
and ``ClientSession`` at the module they're imported from.
"""
from __future__ import annotations

import asyncio
import os
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
async def _fake_stdio_client(params):
    """Stand-in for ``mcp.client.stdio.stdio_client``."""
    _FakeSession.last_init_args = {
        "transport": "stdio",
        "command": params.command,
        "args": list(params.args),
        "env": dict(params.env) if params.env else None,
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
    with pytest.raises(ValueError, match="Unsupported MCP server type"):
        MCPClient({"type": "ftp", "url": "ftp://nope"})


def test_missing_type_rejected():
    with pytest.raises(ValueError, match="Unsupported MCP server type"):
        MCPClient({"url": "http://x"})


def test_env_var_expansion(monkeypatch):
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
    cfg = {"type": "http", "url": "http://x/mcp"}

    async def _run_it():
        client = MCPClient(cfg)
        await client.initialize()
        assert client._initialized is True
        await client.close()
        assert client._initialized is False
        assert client._stack is None
        assert client._session is None
        # Calling close again is a no-op.
        await client.close()

    asyncio.run(_run_it())


def test_call_tool_propagates_errors_as_mcp_error(patched_sdk):
    cfg = {"type": "http", "url": "http://x/mcp"}

    async def _run_it():
        client = MCPClient(cfg)
        with pytest.raises(MCPError, match="tools/call"):
            await client.call_tool("boom", {})
        await client.close()

    asyncio.run(_run_it())


def test_sse_not_implemented(patched_sdk):
    cfg = {"type": "sse", "url": "http://x/sse"}

    async def _run_it():
        client = MCPClient(cfg)
        with pytest.raises(MCPError):
            await client.initialize()

    asyncio.run(_run_it())
