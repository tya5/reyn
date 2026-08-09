"""Tier 2: falsify test — SKILL axis removal is enforcement-safe.

Proves that:
(a) Loading a capability profile with ONLY ``tool_deny`` + ``mcp_deny`` (no
    skill keys) does not crash the loader or resolver.
(b) TOOL enforcement still blocks denied tools after the SKILL axis removal.
(c) MCP enforcement still blocks denied servers after the SKILL axis removal.

This test goes RED if TOOL or MCP enforcement breaks — proving the removal is
safe and the live enforcement axes are intact.

No mocks: real CapabilityProfile / ContextualPermission / ContextualLayer /
load_capability_profile / resolve_profile.
"""
from __future__ import annotations

from pathlib import Path

from reyn.security.permissions.capability_profile import (
    CapabilityProfile,
    load_capability_profile,
    resolve_profile,
)
from reyn.security.permissions.effective import (
    CapabilityAxis,
    ContextualLayer,
)

AX = CapabilityAxis


def test_loader_no_skill_keys_clean(tmp_path: Path) -> None:
    """Tier 2: a profile YAML with only tool_deny + mcp_deny (no skill keys) loads
    cleanly — the loader does not crash without skill keys."""
    p = tmp_path / "no_skill.yaml"
    p.write_text("tool_deny: [danger]\nmcp_deny: [bad-srv]\n", encoding="utf-8")
    prof = load_capability_profile(p)
    assert prof.tool_deny == ("danger",)
    assert prof.mcp_deny == ("bad-srv",)


def test_resolver_no_skill_keys_clean() -> None:
    """Tier 2: resolve_profile on a profile with no skill keys succeeds and
    produces a ContextualPermission with the TOOL/MCP axes populated."""
    prof = CapabilityProfile(name="t", tool_deny=("danger",), mcp_deny=("bad-srv",))
    ctx, excluded = resolve_profile(prof)
    assert "danger" in ctx.tool_deny
    assert "bad-srv" in ctx.mcp_deny
    assert excluded == frozenset()


def test_tool_axis_still_blocks_denied_tool() -> None:
    """Tier 2: ContextualLayer BLOCKS a tool on the TOOL axis after SKILL removal —
    the live enforcement axis is intact."""
    prof = CapabilityProfile(name="t", tool_deny=("danger",))
    ctx, _ = resolve_profile(prof)
    layer = ContextualLayer(ctx)
    assert layer.allows(AX.TOOL, "danger") is False, "denied tool must be blocked"
    assert layer.allows(AX.TOOL, "safe") is True, "non-denied tool must pass"


def test_mcp_axis_still_blocks_denied_server() -> None:
    """Tier 2: ContextualLayer BLOCKS an MCP server on the MCP axis after SKILL
    removal — the live enforcement axis is intact."""
    prof = CapabilityProfile(name="t", mcp_deny=("bad-srv",))
    ctx, _ = resolve_profile(prof)
    layer = ContextualLayer(ctx)
    assert layer.allows(AX.MCP, "bad-srv") is False, "denied mcp server must be blocked"
    assert layer.allows(AX.MCP, "ok-srv") is True, "non-denied server must pass"


def test_combined_profile_tool_and_mcp_enforce() -> None:
    """Tier 2: a profile with ONLY tool_deny + mcp_deny (no skill keys) —
    both axes enforce correctly (the enforcement-safe removal claim)."""
    prof = CapabilityProfile(name="t", tool_deny=("danger",), mcp_deny=("bad-srv",))
    ctx, _ = resolve_profile(prof)
    layer = ContextualLayer(ctx)
    # TOOL axis
    assert layer.allows(AX.TOOL, "danger") is False
    assert layer.allows(AX.TOOL, "safe") is True
    # MCP axis
    assert layer.allows(AX.MCP, "bad-srv") is False
    assert layer.allows(AX.MCP, "ok-srv") is True
