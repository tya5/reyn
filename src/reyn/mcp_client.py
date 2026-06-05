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
import tempfile
import warnings
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
        # #1344: path of the temp Seatbelt profile (.sb) used to sandbox a
        # stdio MCP server's subprocess, if one was generated in _open_stdio.
        # Unlinked in close(). None for non-stdio / non-seatbelt / unsandboxed.
        self._sandbox_profile_path: str | None = None

    @property
    def stderr_capture(self) -> "Any":
        """Read-only accessor for the stderr-capture tempfile (or None).

        Tests inspect this to verify the capture lifecycle (= None
        initially, populated after ``_open_stdio``, None again after
        ``close_stderr_capture``). The write side stays internal so the
        lifecycle stays visible at the call sites that own it.
        """
        return self._stderr_capture

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
            self.close_stderr_capture()
            raise
        except Exception as exc:
            await stack.aclose()
            tail = self.read_stderr_tail()
            self.close_stderr_capture()
            # #1344 migration hint: a sandboxed stdio server runs with network
            # DISABLED by default (secure-by-default). A server that needs the
            # network (e.g. a GitHub MCP) will fail init — point the operator at
            # the `network: true` opt-in knob rather than leave an opaque error.
            hint = ""
            if self._type == "stdio" and not self._config.get("network", False):
                hint = (
                    "\nHint (#1344): this MCP server runs sandboxed with network "
                    "DISABLED by default. If it needs network access, add "
                    "`network: true` to its server config."
                )
            if tail:
                raise MCPError(
                    f"MCP initialize failed: {exc}\n"
                    f"--- subprocess stderr (tail) ---\n{tail}{hint}"
                ) from exc
            raise MCPError(f"MCP initialize failed: {exc}{hint}") from exc

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
            self.close_stderr_capture()
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
        self.close_stderr_capture()

    # ── stderr capture (stdio only) ─────────────────────────────────────────

    STDERR_TAIL_BYTES = 2048

    def read_stderr_tail(self) -> str:
        """Return the tail of the subprocess stderr capture, or ''.

        Reads up to ``STDERR_TAIL_BYTES`` from the end of the temp
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
        if len(text) > self.STDERR_TAIL_BYTES:
            return "...(truncated)\n" + text[-self.STDERR_TAIL_BYTES :]
        return text

    def close_stderr_capture(self) -> None:
        """Close + delete the stderr temp file + the #1344 Seatbelt profile, if
        any. Idempotent — called at every teardown path."""
        # #1344: unlink the temp Seatbelt profile (.sb) generated for a
        # sandboxed stdio MCP server. Best-effort; a leaked temp file must not
        # break teardown.
        profile_path = self._sandbox_profile_path
        if profile_path is not None:
            self._sandbox_profile_path = None
            try:
                os.unlink(profile_path)
            except OSError:
                pass
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

    def _build_mcp_sandbox_policy(self):
        """SandboxPolicy for a sandboxed stdio MCP server (#1344).

        read broad (#1323 scoping) + the default sensitive deny-list; write tight
        to the server's working dir; ``network`` is OPERATOR-declared per server
        (``network: true`` in the MCP config) and defaults OFF — secure-by-default,
        because the sandbox policy is the operator's, not the LLM's. The default-off
        means a network-needing server (e.g. a GitHub MCP) must declare
        ``network: true`` (see the migration hint surfaced on init failure).
        """
        from reyn.sandbox import SandboxPolicy

        cwd = self._config.get("cwd") or os.getcwd()
        return SandboxPolicy(
            network=bool(self._config.get("network", False)),
            write_paths=[cwd],
        )

    def _sandbox_wrap_stdio(self, command: str, args: list[str]) -> "tuple[str, list[str]]":
        """Wrap ``(command, args)`` so the MCP server subprocess runs sandboxed (#1344).

        Seatbelt (macOS): returns ``("sandbox-exec", ["-f", <profile>, command,
        *args])`` with a generated SBPL profile (a temp ``.sb`` unlinked in
        ``close``). MCP stdio is persistent, so the wrap is at the COMMAND level
        (the backend's one-shot ``run()`` does not fit). Other backends
        (landlock/docker) are not yet wrapped here (#1344 follow-up) — the server
        then runs UNSANDBOXED with a warning (never silently).
        """
        from reyn.sandbox import get_default_backend

        try:
            backend = get_default_backend()
            name = getattr(backend, "name", None)
            available = backend.available()
        except Exception:  # noqa: BLE001 — a backend probe must not block a launch
            name, available = None, False
        if name == "seatbelt" and available:
            from reyn.sandbox.backends.seatbelt import _build_sbpl_profile

            profile = _build_sbpl_profile(self._build_mcp_sandbox_policy())
            fh = tempfile.NamedTemporaryFile(
                suffix=".sb", mode="w", delete=False, encoding="utf-8",
            )
            fh.write(profile)
            fh.close()
            self._sandbox_profile_path = fh.name
            return "sandbox-exec", ["-f", fh.name, command, *args]
        warnings.warn(
            f"MCP stdio server {command!r} runs UNSANDBOXED "
            f"(sandbox backend={name or 'none'}); only the Seatbelt wrap is "
            f"implemented (#1344) — Landlock/docker wrapping is a follow-up.",
            stacklevel=2,
        )
        return command, args

    def _open_stdio(self):
        from mcp.client.stdio import StdioServerParameters, stdio_client

        command = self._config.get("command")
        if not command:
            raise MCPError("stdio MCP server config requires 'command'")
        args = list(self._config.get("args") or [])
        # #1344: wrap the server subprocess in the platform sandbox (Seatbelt)
        # so an LLM-invoked MCP tool cannot escape the sandbox via the server.
        command, args = self._sandbox_wrap_stdio(command, args)
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
