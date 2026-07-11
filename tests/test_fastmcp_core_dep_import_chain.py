"""Tier 2: the MCP import chain resolves in a core install (fastmcp is core).

OS invariant:
  ``Session.__init__`` unconditionally imports ``reyn.mcp.connection_service``,
  whose module-level import graph reaches ``fastmcp`` (via
  ``reyn.mcp.message_handler``, which does ``from fastmcp.client.messages import
  MessageHandler`` at module scope). fastmcp was formerly an optional ``[mcp]``
  extra, so a fresh core install without that extra raised
  ``ModuleNotFoundError: No module named 'fastmcp'`` on the first ``reyn chat``.

  fastmcp is now a core dependency, so the MCP client stack must import cleanly
  from a core install. This test exercises the real import chain in a fresh
  subprocess (so fastmcp already present in this test process's ``sys.modules``
  cannot mask a regression) and asserts it does not raise ImportError and that
  the chain actually reached fastmcp.
"""
from __future__ import annotations

import subprocess
import sys


def test_mcp_connection_service_import_chain_reaches_fastmcp():
    """Tier 2: importing the MCP client stack succeeds and loads fastmcp in a fresh interpreter."""
    code = (
        "import reyn.mcp.connection_service;"
        "import reyn.mcp.message_handler;"
        "import reyn.mcp.client;"
        "import sys;"
        "assert 'fastmcp' in sys.modules, "
        "'MCP import chain did not load fastmcp — is it a core dependency?';"
        "print('OK')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, (
        "MCP import chain failed in a core install (fastmcp not importable?).\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "OK" in result.stdout
