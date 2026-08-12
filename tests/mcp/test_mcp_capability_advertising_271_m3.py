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
     with the exact audit-event kinds ``_MCPProgressBridge`` subscribes to
     — asserted on the built ``InitializationOptions``, in
     ``tests/interfaces/test_progress_lifecycle_fanout_3357.py``, since the list is now
     DERIVED from the shared constant rather than restated in source.
  3. ``experimental_capabilities`` declares
     ``reyn.cancellation.cooperative`` (= matches the PR #279 cancel
     wire).
  4. Each declared experimental key is backed by an in-source wire
     (= AST grep: the events / cancel handler exist in ``mcp_server.py``).
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from tests._support.paths import REPO_ROOT

pytest.importorskip("mcp", reason="MCP SDK not installed")


# ── 1. NotificationOptions: lists are static ──────────────────────────


def test_serve_stdio_declares_static_tool_list(tmp_path: Path) -> None:
    """Tier 2: the ``InitializationOptions`` the production pair
    (``build_server`` + ``build_init_options``) advertises declare the tool
    list as STATIC (= what ``_list_tools`` returns never changes at runtime).
    Declaring list-changed without a corresponding ``notify_tools_changed``
    call would be the inverse #267 Z-b mismatch.
    """
    from reyn.core.events.state_log import StateLog
    from reyn.mcp.server import build_init_options, build_server
    from reyn.runtime.registry import AgentRegistry

    def _no_session_expected(profile):  # noqa: ANN001, ANN202
        raise AssertionError("advertising must not construct a Session")

    registry = AgentRegistry(
        project_root=tmp_path,
        session_factory=_no_session_expected,
        state_log=StateLog(tmp_path / ".reyn" / "state" / "wal.jsonl"),
    )
    capabilities = build_init_options(build_server(registry)).capabilities
    assert capabilities.tools is not None
    assert capabilities.tools.list_changed is False


def test_no_notify_changed_calls_in_mcp_server_source() -> None:
    """Tier 2: production source contains no ``notify_*_list_changed``
    call sites (= empirical confirmation that ``tools_changed=False``
    in the declaration is honest).
    """
    src_path = (  # #1682: impl moved to reyn/mcp/server.py (old path = shim)
        REPO_ROOT
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
    ``reyn.progress.skill_lifecycle`` is declared on the real
    ``InitializationOptions`` this server advertises (= the contract between
    declaration and the PR #279 wire).

    The advertised ``events`` list is NOT re-listed here: it is derived from
    ``PROGRESS_LIFECYCLE_EVENTS``, and restating it in a test is what froze two
    producer-less kinds into the wire contract (#3357). The derivation and the
    liveness of its members are asserted in
    ``tests/interfaces/test_progress_lifecycle_fanout_3357.py``.
    """
    from mcp.server import Server

    from reyn.mcp.server import build_init_options

    experimental = build_init_options(Server("reyn")).capabilities.experimental
    assert experimental is not None
    assert "reyn.progress.skill_lifecycle" in experimental


def test_progress_bridge_subscribes_to_declared_event_names() -> None:
    """Tier 2: the kinds advertised in the experimental capability MUST be the
    kinds ``_MCPProgressBridge`` actually filters on — the same object, so the
    declaration cannot drift from the wire.
    """
    from mcp.server import Server

    from reyn.mcp.server import _MCPProgressBridge, build_init_options

    experimental = build_init_options(Server("reyn")).capabilities.experimental
    advertised = experimental["reyn.progress.skill_lifecycle"]["events"]
    assert advertised == sorted(_MCPProgressBridge.TRACKED_EVENTS), (
        "the advertised progress kinds and the bridge's filter disagree "
        "(= #267 Z-b style claim/reality mismatch)."
    )


# ── 3. Experimental: reyn.cancellation.cooperative ───────────────────


def test_experimental_capability_declares_cooperative_cancellation() -> None:
    """Tier 2: the experimental capability key
    ``reyn.cancellation.cooperative`` is declared (= matches PR #279's
    CancelledError propagation wire).
    """
    from mcp.server import Server

    from reyn.mcp.server import build_init_options

    experimental = build_init_options(Server("reyn")).capabilities.experimental
    assert experimental is not None
    assert "reyn.cancellation.cooperative" in experimental


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
        REPO_ROOT
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
