"""Tier 2: MCP server capability advertising (issue #271 M3).

PR #279 wired the actual emit/handle behaviours (= notifications/progress
during send_to_agent + notifications/cancelled propagation). This PR
declares those behaviours in the MCP ``initialize`` response so
clients can negotiate features without trial-and-error.

Calibration constraint (= avoid #267 Z-b "claim vs reality"):
every declared capability must derive from a concrete production wire.
Tests below pin BOTH the declaration AND the wire, so a future PR
that removes one without removing the other fails immediately.

Pins:

  1. ``serve_mcp_stdio_async`` constructs ``init_options`` with the
     expected ``NotificationOptions`` (tools/prompts/resources NOT
     advertised as list-changed — they are static in production).
  2. ``experimental_capabilities`` declares ``reyn.progress.skill_lifecycle``
     with the exact event names that ``_MCPProgressBridge`` subscribes
     to (= phase_started / llm_called / act_executed).
  3. ``experimental_capabilities`` declares
     ``reyn.cancellation.cooperative`` (= matches the PR #279 cancel
     wire).
  4. Each declared experimental key is backed by an in-source wire
     (= AST grep: the events / cancel handler exist in ``mcp_server.py``).
"""
from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

pytest.importorskip("mcp", reason="MCP SDK not installed")


# ── 1. NotificationOptions: lists are static ──────────────────────────


def test_serve_stdio_declares_static_tool_list() -> None:
    """Tier 2: ``serve_mcp_stdio_async`` source carries the explicit
    ``tools_changed=False`` declaration (= the tool list returned by
    ``_list_tools`` is static in production). Declaring True without
    a corresponding ``notify_tools_changed`` call would be the
    inverse #267 Z-b mismatch.
    """
    from reyn.mcp import server as mcp_server

    src = inspect.getsource(mcp_server.serve_stdio)
    assert "tools_changed=False" in src
    assert "prompts_changed=False" in src
    assert "resources_changed=False" in src


def test_no_notify_changed_calls_in_mcp_server_source() -> None:
    """Tier 2: production source contains no ``notify_*_list_changed``
    call sites (= empirical confirmation that ``tools_changed=False``
    in the declaration is honest).
    """
    src_path = (  # #1682: impl moved to reyn/mcp/server.py (old path = shim)
        Path(__file__).parent.parent
        / "src" / "reyn" / "mcp" / "server.py"
    )
    src = src_path.read_text(encoding="utf-8")
    for forbidden in (
        "notify_tools_changed",
        "notify_prompts_changed",
        "notify_resources_changed",
    ):
        assert forbidden not in src, (
            f"{forbidden} call found in mcp_server.py — declaration "
            f"would be a #267 Z-b style claim/reality mismatch. "
            f"Either remove the call or flip the declaration."
        )


# ── 2. Experimental: reyn.progress.skill_lifecycle ───────────────────


def test_experimental_capability_declares_skill_lifecycle_progress() -> None:
    """Tier 2: the experimental capability key
    ``reyn.progress.skill_lifecycle`` is declared with the exact event
    names that ``_MCPProgressBridge`` subscribes to (= the contract
    between declaration and PR #279 wire).
    """
    from reyn.mcp import server as mcp_server

    src = inspect.getsource(mcp_server.serve_stdio)
    assert '"reyn.progress.skill_lifecycle"' in src
    # The event list shape is part of the contract — clients negotiate
    # by reading this field.
    assert '"phase_started"' in src
    assert '"llm_called"' in src
    assert '"act_executed"' in src


def test_progress_bridge_subscribes_to_declared_event_names() -> None:
    """Tier 2: the events advertised in the experimental capability
    MUST match what ``_MCPProgressBridge.__call__`` actually filters
    on. Pin the mapping so a future bridge edit (= e.g. adding /
    removing an event kind) is forced to update the declaration.
    """
    from reyn.mcp import server as mcp_server

    bridge_src = inspect.getsource(mcp_server._MCPProgressBridge)
    for declared_event in ("phase_started", "llm_called", "act_executed"):
        assert f'"{declared_event}"' in bridge_src, (
            f"event {declared_event!r} is declared in the "
            f"experimental capability but _MCPProgressBridge does "
            f"NOT filter on it (= claim/reality mismatch). Either "
            f"add the event filter or drop the declaration."
        )


# ── 3. Experimental: reyn.cancellation.cooperative ───────────────────


def test_experimental_capability_declares_cooperative_cancellation() -> None:
    """Tier 2: the experimental capability key
    ``reyn.cancellation.cooperative`` is declared (= matches PR #279's
    CancelledError propagation wire).
    """
    from reyn.mcp import server as mcp_server

    src = inspect.getsource(mcp_server.serve_stdio)
    assert '"reyn.cancellation.cooperative"' in src


def test_cancellation_wire_exists_in_call_tool_handler() -> None:
    """Tier 2: pin the existence of cancellation-propagation handling
    in the call-tool handler (= the wire backing the declaration).
    AST-search for ``CancelledError`` in ``mcp_server.py`` so a
    refactor that removes the handler is forced to update the
    declaration in the same PR.
    """
    # #1682: the server impl moved to reyn.mcp.server (the old mcp_server.py is
    # now a re-export shim). This source-grep test reads the impl FILE, so it must
    # point at the new path.
    src_path = (
        Path(__file__).parent.parent
        / "src" / "reyn" / "mcp" / "server.py"
    )
    tree = ast.parse(src_path.read_text(encoding="utf-8"))

    cancelled_error_refs = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id == "CancelledError":
            cancelled_error_refs += 1
        elif (
            isinstance(node, ast.Attribute) and node.attr == "CancelledError"
        ):
            cancelled_error_refs += 1

    assert cancelled_error_refs > 0, (
        "No CancelledError reference in mcp_server.py — the "
        "cooperative cancellation capability is declared but the "
        "wire isn't present (= #267 Z-b style claim/reality mismatch)."
    )
