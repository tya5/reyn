"""Tier 2: the MCP import chain resolves in a core install (fastmcp is core).

OS invariant:
  ``Session.__init__`` unconditionally imports ``reyn.mcp.connection_service``,
  which pulls in ``reyn.mcp.client``/``reyn.mcp.message_handler``. fastmcp was
  formerly an optional ``[mcp]`` extra, so a fresh core install without that
  extra raised ``ModuleNotFoundError: No module named 'fastmcp'`` on the first
  ``reyn chat``. fastmcp is now a core dependency, so both (a) the MCP client
  stack must import cleanly from a core install, AND (b) ``fastmcp`` itself
  must actually be importable there — checked separately below.

  #3698 P3 changed HOW (a) and (b) relate: every ``fastmcp`` import in the MCP
  client stack is now deferred (function-local, only executed when a
  connection actually opens — see ``reyn.mcp._fastmcp_boundary``'s module
  docstring) rather than module-level, so importing the stack's TOP-LEVEL
  modules no longer eagerly pulls ``fastmcp`` into ``sys.modules`` as a side
  effect. That was always an incidental proxy for the real invariant ("is
  fastmcp actually installed"), not the invariant itself — checking (b)
  directly, in the same fresh subprocess, is the honest replacement rather
  than re-introducing an eager import somewhere just to keep the old proxy
  working.
"""
from __future__ import annotations

import os
import subprocess
import sys


def test_mcp_import_chain_succeeds_and_fastmcp_is_actually_installed(out_of_process_reyn):
    """Tier 2: (a) the MCP client stack imports cleanly in a fresh interpreter,
    (b) fastmcp itself is importable there — checked directly, not inferred
    from whether reyn's own modules happened to reach it as a side effect."""
    code = (
        "import reyn.mcp.connection_service;"
        "import reyn.mcp.message_handler;"
        "import reyn.mcp.client;"
        "import fastmcp;"
        "print('OK')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=60,
        env={**os.environ, "PYTHONPATH": out_of_process_reyn},
    )
    assert result.returncode == 0, (
        "MCP import chain failed, or fastmcp is not importable, in a core "
        "install.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "OK" in result.stdout
