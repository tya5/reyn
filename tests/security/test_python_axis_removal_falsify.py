"""Tier 2: falsify test — PYTHON axis removal is enforcement-safe.

Proves that:
(a) A PermissionDecl with a stray ``permissions.python:`` key (as a config
    dict) loads cleanly — the loader silently ignores the removed key.
(b) TOOL enforcement still blocks denied tools after the PYTHON axis removal.
(c) MCP enforcement still blocks denied servers after the PYTHON axis removal.

This test goes RED if TOOL or MCP enforcement breaks — proving the removal is
safe and the live enforcement axes are intact.

No mocks: real AgentLayer / EffectivePermission / PermissionDecl / from_dict.
"""
from __future__ import annotations

import pytest

from reyn.security.permissions.effective import (
    AgentLayer,
    CapabilityAxis,
    EffectivePermission,
)
from reyn.security.permissions.permissions import PermissionDecl

AX = CapabilityAxis


def test_loader_stray_python_key_ignored() -> None:
    """Tier 2: a permissions dict with a stray ``python`` key (from pre-removal
    config) loads cleanly without crashing the decl parser."""
    d = {
        "tool": ["grep"],
        "mcp": ["filesystem"],
        "python": [{"module": "./pre.py", "function": "run", "mode": "safe"}],
    }
    decl = PermissionDecl.from_dict(d)
    # tool and mcp are preserved; the stray python key is silently ignored.
    assert decl.tool == ["grep"]
    assert decl.mcp == ["filesystem"]
    # PermissionDecl no longer has a .python field — the loader drops it.
    assert not hasattr(decl, "python")


def test_tool_axis_still_blocks_denied_tool() -> None:
    """Tier 2: AgentLayer BLOCKS a tool on the TOOL axis after PYTHON removal —
    the live enforcement axis is intact."""
    decl = PermissionDecl(tool=["safe_tool"])
    layer = AgentLayer(decl)
    assert layer.allows(AX.TOOL, "safe_tool") is True, "declared tool must pass"
    assert layer.allows(AX.TOOL, "danger_tool") is False, "undeclared tool must be blocked"


def test_mcp_axis_still_blocks_denied_server() -> None:
    """Tier 2: AgentLayer BLOCKS an MCP server on the MCP axis after PYTHON removal —
    the live enforcement axis is intact."""
    decl = PermissionDecl(mcp=["allowed-srv"])
    layer = AgentLayer(decl)
    assert layer.allows(AX.MCP, "allowed-srv") is True, "declared server must pass"
    assert layer.allows(AX.MCP, "denied-srv") is False, "undeclared server must be blocked"


def test_combined_decl_tool_and_mcp_enforce() -> None:
    """Tier 2: a decl with only tool + mcp (no python field) — both axes enforce
    correctly (the enforcement-safe removal claim)."""
    decl = PermissionDecl(tool=["grep"], mcp=["filesystem"])
    layer = AgentLayer(decl)
    # TOOL axis
    assert layer.allows(AX.TOOL, "grep") is True
    assert layer.allows(AX.TOOL, "rm") is False
    # MCP axis
    assert layer.allows(AX.MCP, "filesystem") is True
    assert layer.allows(AX.MCP, "network") is False


def test_python_axis_enum_member_removed() -> None:
    """Tier 2: CapabilityAxis.PYTHON no longer exists after the removal.

    This test goes RED if the enum member is accidentally re-added.
    """
    with pytest.raises(AttributeError):
        _ = CapabilityAxis.PYTHON  # type: ignore[attr-defined]
