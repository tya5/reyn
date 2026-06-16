"""Back-compat shim (#1682): the MCP client transport moved to
``reyn.mcp.client``. Re-exports the public surface so existing
``from reyn.mcp_client import MCPClient, MCPError, expand_env`` call sites keep
working unchanged.

New code should import from ``reyn.mcp`` / ``reyn.mcp.client``.
"""
from __future__ import annotations

from reyn.mcp.client import *  # noqa: F401,F403 — re-export the public surface
from reyn.mcp.client import MCPClient, MCPError, expand_env  # noqa: F401 — explicit
