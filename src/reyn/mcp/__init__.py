"""MCP transport core (#1682).

Consolidates the former top-level ``reyn.mcp_server`` + ``reyn.mcp_client`` and
``reyn.safe.mcp.registry`` into one package:

    reyn.mcp.server    <- mcp_server.py   (build/serve + agent send/list)
    reyn.mcp.client    <- mcp_client.py   (MCPClient transport)
    reyn.mcp.registry  <- safe/mcp/registry.py  (registry lookup)

This ``__init__`` re-exports the curated transport public surface so callers can
``from reyn.mcp import build_server`` / ``MCPClient`` etc. The former module paths
remain as thin re-export shims for back-compat (incl test-private internals).
``reyn.mcp.registry`` is a submodule (its public consumer is the ``reyn.safe.mcp``
allowlist shim).
"""
from __future__ import annotations

from reyn.mcp.client import MCPClient, MCPError, expand_env
from reyn.mcp.server import (
    DEFAULT_SEND_TIMEOUT_SECONDS,
    build_server,
    list_agents_impl,
    send_to_agent_impl,
    serve_stdio,
)

__all__ = [
    "MCPClient",
    "MCPError",
    "expand_env",
    "build_server",
    "list_agents_impl",
    "send_to_agent_impl",
    "serve_stdio",
    "DEFAULT_SEND_TIMEOUT_SECONDS",
]
