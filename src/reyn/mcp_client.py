"""
MCP client — thin wrapper around the official Anthropic ``mcp`` SDK.

Supports two transports today: ``stdio`` and ``http`` (Streamable HTTP).
``sse`` is reserved for a future SDK-backed implementation.

Each ``MCPClient`` owns a persistent ``ClientSession`` opened on
:meth:`initialize` and torn down on :meth:`close`. The session is kept
alive via an ``AsyncExitStack`` so multiple :meth:`call_tool` invocations
re-use the same transport (matching the previous hand-rolled client's
caching semantics on ``OpContext.mcp_clients``).

Environment variable expansion:
  ``${VAR_NAME}`` in any string config value is replaced with
  ``os.environ.get("VAR_NAME", "")``. Missing variables expand to empty
  string and a warning is emitted. Apply :func:`expand_env` BEFORE
  handing config to the SDK.
"""
from __future__ import annotations

import os
import re
import warnings
from contextlib import AsyncExitStack
from typing import Any


# ── Env var expansion ─────────────────────────────────────────────────────────

_ENV_VAR_RE = re.compile(r"\$\{(\w+)\}")


def _expand_str(value: str) -> str:
    def _replace(m: re.Match) -> str:
        name = m.group(1)
        result = os.environ.get(name)
        if result is None:
            warnings.warn(
                f"MCP config references undefined environment variable: ${{{name}}}",
                stacklevel=4,
            )
            return ""
        return result
    return _ENV_VAR_RE.sub(_replace, value)


def expand_env(obj: Any) -> Any:
    """Recursively expand ${VAR} in all string values of a dict/list/str."""
    if isinstance(obj, str):
        return _expand_str(obj)
    if isinstance(obj, dict):
        return {k: expand_env(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [expand_env(i) for i in obj]
    return obj


# ── Errors ───────────────────────────────────────────────────────────────────

class MCPError(RuntimeError):
    """Raised on any MCP transport / protocol / tool error."""


_SUPPORTED_TYPES = {"stdio", "http", "sse"}


# ── Client ───────────────────────────────────────────────────────────────────

class MCPClient:
    """Thin async wrapper around ``mcp.ClientSession``.

    Construct with the *raw* server config dict from ``reyn.yaml`` (the
    caller is responsible for env-var expansion via :func:`expand_env`).

    Lifecycle::

        client = MCPClient(cfg)
        await client.initialize()
        result = await client.call_tool("read_file", {"path": "x"})
        await client.close()
    """

    def __init__(self, config: dict[str, Any]) -> None:
        if not isinstance(config, dict):
            raise ValueError(f"MCP server config must be a dict, got {type(config).__name__}")
        srv_type = config.get("type")
        if srv_type not in _SUPPORTED_TYPES:
            raise ValueError(
                f"Unsupported MCP server type: {srv_type!r}. "
                f"Expected one of {sorted(_SUPPORTED_TYPES)}."
            )
        self._config: dict[str, Any] = dict(config)
        self._type: str = srv_type
        self._stack: AsyncExitStack | None = None
        self._session: Any = None  # mcp.ClientSession when initialized
        self._initialized = False

    # ── public API ──────────────────────────────────────────────────────────

    def is_initialized(self) -> bool:
        """Return True if the MCP session is currently open.

        Read-only query used by tests to assert lifecycle state without
        accessing private attributes directly.
        """
        return self._initialized

    async def initialize(self) -> None:
        """Open the transport and complete the MCP handshake.

        Idempotent: a second call is a no-op.
        """
        if self._initialized:
            return
        try:
            from mcp import ClientSession
        except ImportError as exc:
            raise MCPError(
                "The 'mcp' package is required for MCP support. "
                "Install with: pip install reyn[mcp]"
            ) from exc

        stack = AsyncExitStack()
        try:
            transport = await stack.enter_async_context(self._open_transport())
            # streamablehttp_client yields (read, write, get_session_id);
            # stdio_client yields (read, write).
            read_stream, write_stream = transport[0], transport[1]
            session = await stack.enter_async_context(
                ClientSession(read_stream, write_stream)
            )
            await session.initialize()
        except MCPError:
            await stack.aclose()
            raise
        except Exception as exc:
            await stack.aclose()
            raise MCPError(f"MCP initialize failed: {exc}") from exc

        self._stack = stack
        self._session = session
        self._initialized = True

    async def call_tool(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        """Call ``name`` on the server with ``args``. Returns a dict
        shaped to match what ``op_runtime/mcp.py`` consumes:
        ``{"content": [...], "isError": bool, "structuredContent": ... | None}``.
        """
        await self.initialize()
        try:
            result = await self._session.call_tool(name, args or {})
        except Exception as exc:
            raise MCPError(f"MCP tools/call error: {exc}") from exc
        return _result_to_dict(result)

    async def list_tools(self) -> list[dict[str, Any]]:
        """Return the tools advertised by this server as plain dicts."""
        await self.initialize()
        try:
            result = await self._session.list_tools()
        except Exception as exc:
            raise MCPError(f"MCP tools/list error: {exc}") from exc
        return [_tool_to_dict(t) for t in result.tools]

    async def close(self) -> None:
        """Tear down the transport and session. Safe to call repeatedly."""
        if self._stack is None:
            return
        stack = self._stack
        self._stack = None
        self._session = None
        self._initialized = False
        try:
            await stack.aclose()
        except Exception:
            # Best-effort cleanup; transport may already be down.
            pass

    # ── transport dispatch ──────────────────────────────────────────────────

    def _open_transport(self):
        if self._type == "stdio":
            return self._open_stdio()
        if self._type == "http":
            return self._open_http()
        if self._type == "sse":
            return self._open_sse()
        # Unreachable due to __init__ validation, but keep defensive.
        raise ValueError(f"Unsupported MCP server type: {self._type!r}")

    def _open_stdio(self):
        from mcp.client.stdio import StdioServerParameters, stdio_client

        command = self._config.get("command")
        if not command:
            raise MCPError("stdio MCP server config requires 'command'")
        args = list(self._config.get("args") or [])
        env = self._config.get("env")
        params = StdioServerParameters(
            command=command,
            args=args,
            env=dict(env) if env else None,
            cwd=self._config.get("cwd"),
        )
        return stdio_client(params)

    def _open_http(self):
        from mcp.client.streamable_http import streamablehttp_client

        url = self._config.get("url")
        if not url:
            raise MCPError("http MCP server config requires 'url'")
        headers = {
            str(k): str(v) for k, v in (self._config.get("headers") or {}).items()
        }
        timeout = self._config.get("timeout", 30)
        return streamablehttp_client(url, headers=headers, timeout=timeout)

    def _open_sse(self):
        # The SDK ships sse_client but we defer wiring it until we have a
        # real test target. Surface a clean error instead of half-supporting.
        raise NotImplementedError(
            "MCP 'sse' transport is not yet implemented. Use 'stdio' or 'http'."
        )


# ── Backward-compat shim ─────────────────────────────────────────────────────

class MCPHTTPClient(MCPClient):
    """Deprecated alias for :class:`MCPClient` configured for HTTP.

    Preserved for any out-of-tree caller that imported the old class
    directly. New code should use ``MCPClient({"type": "http", ...})``.
    """

    def __init__(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        timeout: int = 30,
    ) -> None:
        super().__init__(
            {
                "type": "http",
                "url": url,
                "headers": dict(headers or {}),
                "timeout": timeout,
            }
        )


# ── helpers ──────────────────────────────────────────────────────────────────

def _result_to_dict(result: Any) -> dict[str, Any]:
    """Flatten an ``mcp.types.CallToolResult`` into the shape
    ``op_runtime/mcp.py`` expects (mirrors the JSON-RPC ``result`` field of
    the previous hand-rolled client)."""
    content_items = []
    for item in getattr(result, "content", []) or []:
        # Each item is a TextContent / ImageContent / etc. pydantic model.
        if hasattr(item, "model_dump"):
            content_items.append(item.model_dump())
        elif isinstance(item, dict):
            content_items.append(item)
        else:
            content_items.append({"type": "text", "text": str(item)})
    return {
        "content": content_items,
        "isError": bool(getattr(result, "isError", False)),
        "structuredContent": getattr(result, "structuredContent", None),
    }


def _tool_to_dict(tool: Any) -> dict[str, Any]:
    if hasattr(tool, "model_dump"):
        return tool.model_dump()
    return dict(tool)
