"""Tier 1: #2074 S1 — the unified capability spec carries TOOL / MCP axes.

S1 extends the capability spec (CapabilityProfile) + its resolver/compose + loader
with the MCP axis alongside the existing TOOL / category axes, so ONE spec covers
TOOL and MCP axes. The SKILL axis was removed (dead — zero live enforcement).

No mocks: real CapabilityProfile / ContextualPermission / ContextualLayer + the
pure resolver/compose/loader functions.
"""
from __future__ import annotations

from pathlib import Path

from reyn.security.permissions.capability_profile import (
    CapabilityProfile,
    compose_resolved,
    load_capability_profile,
    resolve_profile,
)
from reyn.security.permissions.effective import (
    CapabilityAxis,
    ContextualLayer,
    ContextualPermission,
)

AX = CapabilityAxis


# ── resolve: the MCP axis flows onto the ContextualPermission ───────────────


def test_resolve_populates_mcp_axes() -> None:
    """Tier 1: resolve_profile carries mcp allow/deny onto the ContextualPermission."""
    prof = CapabilityProfile(
        name="p",
        mcp_allow=("srv1",),
        mcp_deny=("srv2",),
    )
    ctx, _excluded = resolve_profile(prof)
    assert ctx.mcp_allow == frozenset({"srv1"})
    assert ctx.mcp_deny == frozenset({"srv2"})


def test_resolve_mcp_default_inert() -> None:
    """Tier 1: a profile with no mcp keys resolves to ⊤ on those axes
    (allow=None, deny=∅) — the additive default narrows nothing."""
    ctx, _ = resolve_profile(CapabilityProfile(name="p", tool_deny=("t",)))
    assert ctx.mcp_allow is None and ctx.mcp_deny == frozenset()
    # the existing TOOL axis is unaffected
    assert ctx.tool_deny == frozenset({"t"})


# ── loader: forward-compat parse of the new keys ────────────────────────────


def test_loader_parses_mcp_keys(tmp_path: Path) -> None:
    """Tier 1: the loader parses mcp_allow/mcp_deny."""
    p = tmp_path / "p.yaml"
    p.write_text(
        "mcp_allow: [m1]\nmcp_deny: [m2]\n",
        encoding="utf-8",
    )
    prof = load_capability_profile(p)
    assert prof.mcp_allow == ("m1",)
    assert prof.mcp_deny == ("m2",)


def test_loader_absent_mcp_is_none_empty(tmp_path: Path) -> None:
    """Tier 1: absent mcp keys → None/() (forward-compat; pre-#2074 yaml unaffected)."""
    p = tmp_path / "p.yaml"
    p.write_text("tool_deny: [foo]\n", encoding="utf-8")
    prof = load_capability_profile(p)
    assert prof.mcp_allow is None and prof.mcp_deny == ()


def test_loader_ignores_skill_keys(tmp_path: Path) -> None:
    """Tier 1: skill_allow/skill_deny in a YAML file are ignored (forward-compat
    — the SKILL axis was removed; old files with skill keys load cleanly)."""
    p = tmp_path / "p.yaml"
    p.write_text(
        "skill_allow: [x, y]\nskill_deny: [z]\nmcp_allow: [m1]\nmcp_deny: [m2]\n",
        encoding="utf-8",
    )
    prof = load_capability_profile(p)
    assert prof.mcp_allow == ("m1",)
    assert prof.mcp_deny == ("m2",)


# ── compose: the same monotonic rule applies to MCP ────────────────────────


def test_compose_unions_denies_and_intersects_allows_mcp() -> None:
    """Tier 1: compose_resolved applies union-of-deny + intersection-of-allow to the
    MCP axis (most-restrictive-wins across profiles)."""
    a = resolve_profile(CapabilityProfile(name="a", mcp_deny=("m1",)))
    b = resolve_profile(CapabilityProfile(name="b", mcp_deny=("m2",)))
    composed, _excluded = compose_resolved([a, b])
    # deny → union
    assert composed.mcp_deny == frozenset({"m1", "m2"})
    # mcp_allow: neither constrained → ⊤
    assert composed.mcp_allow is None


def test_compose_empty_is_inert() -> None:
    """Tier 1: composing nothing yields an inert all-⊤ ContextualPermission."""
    composed, excluded = compose_resolved([])
    assert composed.mcp_allow is None and composed.mcp_deny == frozenset()
    assert composed.tool_allow is None and composed.tool_deny == frozenset()
    assert excluded == frozenset()


# ── TOOL enforcement: ContextualLayer still enforces TOOL ──────────────────


def test_contextual_layer_still_enforces_tool() -> None:
    """Tier 1: TOOL enforcement is unchanged (no regression on the live axis)."""
    layer = ContextualLayer(ContextualPermission(tool_deny=frozenset({"danger"})))
    assert layer.allows(AX.TOOL, "danger") is False
    assert layer.allows(AX.TOOL, "safe") is True
