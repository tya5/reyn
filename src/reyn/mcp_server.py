"""Back-compat shim (#1682): the MCP server transport core moved to
``reyn.mcp.server``. This re-exports the public surface AND the test-private
internals (8 test files import ``_MCPProgressBridge`` / ``_MCPInterventionBus`` /
``_get_agent_lock``) so existing ``from reyn.mcp_server import …`` and
``from reyn import mcp_server`` call sites keep working unchanged.

New code should import from ``reyn.mcp`` / ``reyn.mcp.server``.
"""
from __future__ import annotations

from reyn.mcp.server import *  # noqa: F401,F403 — re-export the public surface (__all__)
from reyn.mcp.server import (  # noqa: F401 — underscore internals (not in __all__)
    _MCPInterventionBus,
    _MCPProgressBridge,
    _get_agent_lock,
)
