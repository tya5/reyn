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

import tempfile
from contextlib import AsyncExitStack
from typing import Any

# ── Env var expansion ─────────────────────────────────────────────────────────
# Shared resolver lives in reyn.secrets.interpolation (ADR-0030).
# This re-export keeps the public surface of this module backward-compatible:
# callers that import ``from reyn.mcp_client import expand_env`` continue to
# work without change.
from reyn.secrets.interpolation import expand_env as expand_env  # noqa: F401

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

    def __init__(
        self,
        config: dict[str, Any],
        *,
        agent_id: str | None = None,
    ) -> None:
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
        # FP-0016 Component E: agent_id is injected as the
        # ``X-Reyn-Agent-Id`` header on every outgoing HTTP request so
        # downstream MCP servers can attribute calls to a specific Reyn
        # agent. None preserves prior behaviour for direct callers (= the
        # session factory passes ReynConfig.agent.id; tests can omit).
        self._agent_id: str | None = agent_id
        self._stack: AsyncExitStack | None = None
        self._session: Any = None  # mcp.ClientSession when initialized
        self._initialized = False
        # Captures subprocess stderr for stdio transport so initialize
        # failures (e.g. self-made MCP server exits immediately, writes
        # a traceback to stderr before the MCP handshake completes) can
        # surface the actual error text rather than the opaque "Connection
        # close" wording the SDK produces. mcp SDK's ``stdio_client``
        # passes errlog directly to ``anyio.open_process(stderr=...)``,
        # which needs a real fileno — ``io.StringIO`` doesn't work, but
        # ``tempfile.TemporaryFile`` does. Lazily created in
        # ``_open_stdio``; closed in ``close``.
        self._stderr_capture: Any = None  # tempfile.TemporaryFile | None

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
            self._close_stderr_capture()
            raise
        except Exception as exc:
            await stack.aclose()
            tail = self._read_stderr_tail()
            self._close_stderr_capture()
            if tail:
                raise MCPError(
                    f"MCP initialize failed: {exc}\n"
                    f"--- subprocess stderr (tail) ---\n{tail}"
                ) from exc
            raise MCPError(f"MCP initialize failed: {exc}") from exc

        self._stack = stack
        self._session = session
        self._initialized = True

    async def call_tool(
        self,
        name: str,
        args: dict[str, Any],
        *,
        progress_callback: Any = None,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        """Call ``name`` on the server with ``args``. Returns a dict
        shaped to match what ``op_runtime/mcp.py`` consumes:
        ``{"content": [...], "isError": bool, "structuredContent": ... | None}``.

        Optional kwargs (issue #264 — wire SDK long-running support):

          - ``progress_callback``: async ``(progress: float, total: float | None,
            message: str | None) -> None`` that the MCP SDK invokes when the
            server emits a ``notifications/progress`` for this call. Default
            ``None`` matches pre-#264 behaviour (= no progress visibility).
          - ``timeout_seconds``: float; if set, converts to ``timedelta`` and
            passes as ``read_timeout_seconds`` to the SDK so the call fails
            fast on a stuck server. Default ``None`` keeps the SDK's own
            transport-level default.
        """
        await self.initialize()
        kwargs: dict[str, Any] = {}
        if progress_callback is not None:
            kwargs["progress_callback"] = progress_callback
        if timeout_seconds is not None:
            from datetime import timedelta
            kwargs["read_timeout_seconds"] = timedelta(seconds=timeout_seconds)
        try:
            result = await self._session.call_tool(name, args or {}, **kwargs)
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
            self._close_stderr_capture()
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
        self._close_stderr_capture()

    # ── stderr capture (stdio only) ─────────────────────────────────────────

    _STDERR_TAIL_BYTES = 2048

    def _read_stderr_tail(self) -> str:
        """Return the tail of the subprocess stderr capture, or ''.

        Reads up to ``_STDERR_TAIL_BYTES`` from the end of the temp
        file. Returns empty string when no capture is configured (= http
        transport, or stdio capture failed to open) or read raises.
        Failures here are advisory: never propagate beyond the helper
        so the caller's MCPError carries the original exception even
        if the tail can't be retrieved.
        """
        capture = self._stderr_capture
        if capture is None:
            return ""
        try:
            capture.flush()
        except Exception:  # noqa: BLE001
            pass
        try:
            capture.seek(0)
            data = capture.read()
        except Exception:  # noqa: BLE001
            return ""
        if not data:
            return ""
        if isinstance(data, bytes):
            try:
                text = data.decode("utf-8", errors="replace")
            except Exception:  # noqa: BLE001
                return ""
        else:
            text = data
        if len(text) > self._STDERR_TAIL_BYTES:
            return "...(truncated)\n" + text[-self._STDERR_TAIL_BYTES :]
        return text

    def _close_stderr_capture(self) -> None:
        """Close + delete the stderr temp file, if any. Idempotent."""
        capture = self._stderr_capture
        if capture is None:
            return
        self._stderr_capture = None
        try:
            capture.close()
        except Exception:  # noqa: BLE001
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
        # Subprocess stderr capture for diagnostic readback on init
        # failure. ``stdio_client`` passes errlog to
        # ``anyio.open_process(stderr=...)`` which requires a real
        # fileno — ``io.StringIO`` doesn't work. ``tempfile.TemporaryFile``
        # auto-deletes on close. Text-mode + utf-8 matches the SDK's
        # default (= sys.stderr). On failure to open the temp file we
        # fall through to no capture (= behavior degrades gracefully
        # to the pre-fix opaque error wording, never blocks the call).
        try:
            self._stderr_capture = tempfile.TemporaryFile(
                mode="w+t", encoding="utf-8",
            )
        except Exception:  # noqa: BLE001 — temp-file failure is non-fatal
            self._stderr_capture = None
            return stdio_client(params)
        return stdio_client(params, errlog=self._stderr_capture)

    def _open_http(self):
        """Open the Streamable HTTP transport.

        Reads from ``self._config``:
          - ``url`` (required) — full MCP endpoint URL.
          - ``headers`` (optional dict[str, str]) — HTTP headers sent on
            every request to the server. Used for ``Authorization: Bearer
            <token>`` and API-key style auth required by hosted MCP servers
            (GitHub MCP, Atlassian MCP, internal enterprise MCPs).  This is
            FP-0016 Component A. Values are passed through verbatim;
            ``${VAR}`` interpolation is the caller's responsibility (the
            standard load_config path applies ``expand_env`` recursively
            across the whole merged config — see ADR-0030).
          - ``timeout`` (optional, default 30) — request timeout in seconds.
        """
        from mcp.client.streamable_http import streamablehttp_client

        url = self._config.get("url")
        if not url:
            raise MCPError("http MCP server config requires 'url'")
        headers = {
            str(k): str(v) for k, v in (self._config.get("headers") or {}).items()
        }
        # FP-0016 Component E: inject the agent_id as X-Reyn-Agent-Id so
        # downstream MCP servers can attribute requests to a specific
        # Reyn agent (= RBAC + audit trail requirement per the issue
        # シナリオ 5 Enterprise Agent ID pattern). Explicit operator
        # headers win when they already set the field — operators may
        # need to spoof in tests or proxy in production.
        if self._agent_id and "X-Reyn-Agent-Id" not in headers:
            headers["X-Reyn-Agent-Id"] = self._agent_id
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
