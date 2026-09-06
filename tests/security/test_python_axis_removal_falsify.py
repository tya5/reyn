"""Tier 2: falsify test — PYTHON axis removal is enforcement-safe.

Proves that:
(a) A PermissionDecl with stray ``permissions.python:``/``permissions.tool:``
    keys (as a config dict) loads cleanly — the loader silently ignores
    both removed keys (PYTHON: #1199; TOOL: #5848 — decl.tool has zero
    production populators and its empty-list-means-deny-all semantics
    were the OPPOSITE of ContextualLayer's own reading of the same axis).
(b) MCP enforcement still blocks denied servers after the PYTHON axis removal.

This test goes RED if MCP enforcement breaks — proving the removal is safe
and the live enforcement axis is intact. TOOL's own equivalent control
(AgentLayer is now UNCONSTRAINED on that axis, not enforcing) lives in
``test_1199_s31b2c_oprt_gates.py``'s ``test_tool_axis_is_unconstrained_by_
agent_layer`` — this file no longer claims TOOL enforcement, since #5848
retired it.

No mocks: real AgentLayer / EffectivePermission / PermissionDecl / from_dict.
"""
from __future__ import annotations

import pytest

from reyn.security.permissions.effective import (
    AgentLayer,
    CapabilityAxis,
)
from reyn.security.permissions.permissions import PermissionDecl

AX = CapabilityAxis


def test_loader_stray_python_and_tool_keys_ignored() -> None:
    """Tier 2: a permissions dict with stray ``python``/``tool`` keys (from
    pre-removal config) loads cleanly without crashing the decl parser."""
    d = {
        "tool": ["grep"],
        "mcp": ["filesystem"],
        "python": [{"module": "./pre.py", "function": "run", "mode": "safe"}],
    }
    decl = PermissionDecl.from_dict(d)
    # mcp is preserved; the stray python AND tool keys are both silently
    # ignored (#5848 removed the .tool field the same way PYTHON's
    # removal already dropped .python).
    assert decl.mcp == ["filesystem"]
    assert not hasattr(decl, "python")
    assert not hasattr(decl, "tool")


def test_mcp_axis_still_blocks_denied_server() -> None:
    """Tier 2: AgentLayer BLOCKS an MCP server on the MCP axis after PYTHON removal —
    the live enforcement axis is intact."""
    decl = PermissionDecl(mcp=["allowed-srv"])
    layer = AgentLayer(decl)
    assert layer.allows(AX.MCP, "allowed-srv") is True, "declared server must pass"
    assert layer.allows(AX.MCP, "denied-srv") is False, "undeclared server must be blocked"


def test_python_axis_enum_member_removed() -> None:
    """Tier 2: CapabilityAxis.PYTHON no longer exists after the removal.

    This test goes RED if the enum member is accidentally re-added.
    """
    with pytest.raises(AttributeError):
        _ = CapabilityAxis.PYTHON  # type: ignore[attr-defined]
