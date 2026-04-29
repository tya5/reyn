"""
MCP HTTP client — implements the MCP Streamable HTTP transport.

Protocol flow per server (stateless across runs, session cached within a run):
  1. POST /mcp  {"method": "initialize", ...}   → receive Mcp-Session-Id header (if any)
  2. POST /mcp  {"method": "tools/call", ...}   → receive tool result

Environment variable expansion:
  ${VAR_NAME} in any string config value is replaced with os.environ.get("VAR_NAME", "").
  Missing variables expand to empty string and a warning is printed.
"""
from __future__ import annotations

import os
import re
import warnings
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


# ── MCP HTTP client ───────────────────────────────────────────────────────────

class MCPError(RuntimeError):
    pass


class MCPHTTPClient:
    """
    Minimal MCP HTTP client for Streamable HTTP transport.

    One instance per server per run — caches the session ID from `initialize`.
    Thread safety: not needed (Reyn is single-threaded).
    """

    PROTOCOL_VERSION = "2024-11-05"

    def __init__(self, url: str, headers: dict[str, str], timeout: int = 30) -> None:
        self._url = url
        self._headers = {"Content-Type": "application/json", **headers}
        self._timeout = timeout
        self._session_id: str | None = None
        self._initialized = False
        self._req_id = 0

    def _next_id(self) -> int:
        self._req_id += 1
        return self._req_id

    def _post(self, body: dict) -> dict:
        import httpx

        hdrs = dict(self._headers)
        if self._session_id:
            hdrs["Mcp-Session-Id"] = self._session_id

        try:
            response = httpx.post(
                self._url, json=body, headers=hdrs, timeout=self._timeout
            )
        except httpx.TimeoutException:
            raise MCPError(f"MCP request timed out after {self._timeout}s (url: {self._url})")
        except httpx.RequestError as exc:
            raise MCPError(f"MCP request failed: {exc}")

        if response.status_code >= 400:
            raise MCPError(
                f"MCP server returned HTTP {response.status_code}: {response.text[:200]}"
            )

        # Capture session ID on first response
        if not self._session_id:
            sid = response.headers.get("Mcp-Session-Id")
            if sid:
                self._session_id = sid

        try:
            return response.json()
        except Exception:
            raise MCPError(f"MCP server returned non-JSON response: {response.text[:200]}")

    def initialize(self) -> None:
        """Perform MCP handshake. Idempotent within a run."""
        if self._initialized:
            return
        body = {
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": "initialize",
            "params": {
                "protocolVersion": self.PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "reyn", "version": "1.0"},
            },
        }
        result = self._post(body)
        if "error" in result:
            raise MCPError(f"MCP initialize failed: {result['error']}")
        self._initialized = True

    def call_tool(self, tool: str, args: dict) -> dict:
        """
        Call a tool on the MCP server.
        Returns the raw result dict from the MCP response.
        Raises MCPError on protocol or tool errors.
        """
        self.initialize()
        body = {
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": "tools/call",
            "params": {"name": tool, "arguments": args},
        }
        response = self._post(body)

        if "error" in response:
            raise MCPError(f"MCP tools/call error: {response['error']}")

        result = response.get("result", {})
        return result

    def list_tools(self) -> list[dict]:
        """Return the list of tools available on this server."""
        self.initialize()
        body = {
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": "tools/list",
        }
        response = self._post(body)
        if "error" in response:
            raise MCPError(f"MCP tools/list error: {response['error']}")
        return response.get("result", {}).get("tools", [])
