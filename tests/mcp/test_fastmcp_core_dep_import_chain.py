"""Tier 2: the MCP import chain resolves in a core install (mcp is core; fastmcp is not).

OS invariant:
  ``Session.__init__`` unconditionally imports ``reyn.mcp.connection_service``,
  which pulls in ``reyn.mcp.client``/``reyn.mcp.message_handler``. Historically
  this chain required fastmcp (formerly an optional ``[mcp]`` extra, then a
  core dependency), so a fresh install without it raised
  ``ModuleNotFoundError: No module named 'fastmcp'`` on the first ``reyn
  chat``. #4302: fastmcp is DROPPED entirely (the client path moved onto the
  official ``mcp`` SDK directly in #4282/#4299; the last fastmcp-server
  test-doubles were ported in #4302 itself) — the invariant this test pins is
  now (a) the MCP client stack imports cleanly from a core install, AND (b)
  ``mcp`` itself (the actual core dependency, pinned ``>=2.0,<3.0`` — #4412
  bumped this off the original ``>=1.24,<2.0`` floor #4302 set; see #4412 for
  why that upper bound existed and #4368 for the server-side port that
  lifted it) is importable there, AND (c) ``fastmcp`` is
  explicitly NOT installed as a side effect of a core install — a positive
  assertion that the drop actually took, not just "didn't check".

  #3698 P3 changed HOW (a) and the old (b) related: every ``fastmcp`` import
  in the MCP client stack was deferred (function-local) before being removed
  outright, so importing the stack's TOP-LEVEL modules never eagerly pulled
  fastmcp into ``sys.modules`` as a side effect — checking installedness
  directly, in the same fresh subprocess, was always the honest invariant
  rather than an eager-import proxy for it.
"""
from __future__ import annotations

import os
import subprocess
import sys


def test_mcp_import_chain_succeeds_and_fastmcp_is_not_installed(out_of_process_reyn):
    """Tier 2: (a) the MCP client stack imports cleanly in a fresh interpreter,
    (b) ``mcp`` (the real core dependency) is importable there, (c) ``fastmcp``
    is NOT importable there — #4302 dropped it; a core install pulling it back
    in (e.g. via a stray extras/transitive dependency) would be a regression
    this catches directly, not inferred from reyn's own modules never
    reaching it."""
    code = (
        "import reyn.mcp.connection_service;"
        "import reyn.mcp.message_handler;"
        "import reyn.mcp.client;"
        "import mcp;"
        "import fastmcp;"
        "print('FASTMCP_PRESENT')"
    )
    # #4397: no timeout= — CI's own per-test pytest-timeout is the kill switch.
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": out_of_process_reyn},
    )
    assert result.returncode != 0, (
        "fastmcp is importable in a core install — #4302 dropped it as a "
        "core dependency; if this now succeeds, something reintroduced it "
        "(a stray extra, a transitive pull-in, or a revert).\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "ModuleNotFoundError" in result.stderr and "fastmcp" in result.stderr

    # (a)+(b): the real chain, without the fastmcp import, must succeed cleanly.
    code_ok = (
        "import reyn.mcp.connection_service;"
        "import reyn.mcp.message_handler;"
        "import reyn.mcp.client;"
        "import mcp;"
        "print('OK')"
    )
    result_ok = subprocess.run(
        [sys.executable, "-c", code_ok],
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": out_of_process_reyn},
    )
    assert result_ok.returncode == 0, (
        "MCP import chain failed, or mcp is not importable, in a core install.\n"
        f"stdout: {result_ok.stdout}\nstderr: {result_ok.stderr}"
    )
    assert "OK" in result_ok.stdout
