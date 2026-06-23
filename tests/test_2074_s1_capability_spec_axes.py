"""Tier 1: #2074 S1 — the unified capability spec gains SKILL / MCP axes (additive, inert).

S1 extends the capability spec (CapabilityProfile) + its resolver/compose + loader
with the SKILL and MCP axes alongside the existing TOOL / category axes, so ONE
spec covers all #1199 ∩ axes. It is strictly **additive and inert**: the new axes
are carried on the resolved ContextualPermission but ``ContextualLayer`` still
enforces only TOOL (the SKILL/MCP binding is #2074 S2/S3). So a profile that sets
skill/mcp narrowing changes NO gate outcome yet — proven here.

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


# ── resolve: the new axes flow onto the ContextualPermission ────────────────


def test_resolve_populates_skill_and_mcp_axes() -> None:
    """Tier 1: resolve_profile carries skill/mcp allow/deny onto the ContextualPermission."""
    prof = CapabilityProfile(
        name="p",
        skill_allow=("a", "b"),
        skill_deny=("c",),
        mcp_allow=("srv1",),
        mcp_deny=("srv2",),
    )
    ctx, _excluded = resolve_profile(prof)
    assert ctx.skill_allow == frozenset({"a", "b"})
    assert ctx.skill_deny == frozenset({"c"})
    assert ctx.mcp_allow == frozenset({"srv1"})
    assert ctx.mcp_deny == frozenset({"srv2"})


def test_resolve_skill_mcp_default_inert() -> None:
    """Tier 1: a profile with no skill/mcp keys resolves to ⊤ on those axes
    (allow=None, deny=∅) — the additive default narrows nothing."""
    ctx, _ = resolve_profile(CapabilityProfile(name="p", tool_deny=("t",)))
    assert ctx.skill_allow is None and ctx.skill_deny == frozenset()
    assert ctx.mcp_allow is None and ctx.mcp_deny == frozenset()
    # the existing TOOL axis is unaffected
    assert ctx.tool_deny == frozenset({"t"})


# ── loader: forward-compat parse of the new keys ────────────────────────────


def test_loader_parses_skill_mcp_keys(tmp_path: Path) -> None:
    """Tier 1: the loader parses skill_allow/skill_deny/mcp_allow/mcp_deny."""
    p = tmp_path / "p.yaml"
    p.write_text(
        "skill_allow: [x, y]\nskill_deny: [z]\nmcp_allow: [m1]\nmcp_deny: [m2]\n",
        encoding="utf-8",
    )
    prof = load_capability_profile(p)
    assert prof.skill_allow == ("x", "y")
    assert prof.skill_deny == ("z",)
    assert prof.mcp_allow == ("m1",)
    assert prof.mcp_deny == ("m2",)


def test_loader_absent_skill_mcp_is_none_empty(tmp_path: Path) -> None:
    """Tier 1: absent skill/mcp keys → None/() (forward-compat; pre-#2074 yaml unaffected)."""
    p = tmp_path / "p.yaml"
    p.write_text("tool_deny: [foo]\n", encoding="utf-8")
    prof = load_capability_profile(p)
    assert prof.skill_allow is None and prof.skill_deny == ()
    assert prof.mcp_allow is None and prof.mcp_deny == ()


# ── compose: the same monotonic rule applies to the new axes ────────────────


def test_compose_unions_denies_and_intersects_allows_per_axis() -> None:
    """Tier 1: compose_resolved applies union-of-deny + intersection-of-allow to the
    SKILL/MCP axes, identically to TOOL (most-restrictive-wins across profiles)."""
    a = resolve_profile(CapabilityProfile(name="a", skill_allow=("x", "y"), skill_deny=("d1",), mcp_deny=("m1",)))
    b = resolve_profile(CapabilityProfile(name="b", skill_allow=("y", "z"), skill_deny=("d2",), mcp_deny=("m2",)))
    composed, _excluded = compose_resolved([a, b])
    # allow → intersection
    assert composed.skill_allow == frozenset({"y"})
    # deny → union
    assert composed.skill_deny == frozenset({"d1", "d2"})
    assert composed.mcp_deny == frozenset({"m1", "m2"})
    # mcp_allow: neither constrained → ⊤
    assert composed.mcp_allow is None


def test_compose_empty_is_inert() -> None:
    """Tier 1: composing nothing yields an inert all-⊤ ContextualPermission."""
    composed, excluded = compose_resolved([])
    assert composed.skill_allow is None and composed.skill_deny == frozenset()
    assert composed.mcp_allow is None and composed.mcp_deny == frozenset()
    assert composed.tool_allow is None and composed.tool_deny == frozenset()
    assert excluded == frozenset()


# ── INERTNESS: ContextualLayer still enforces ONLY TOOL (S1 changes no gate) ─


def test_contextual_layer_does_not_yet_enforce_mcp() -> None:
    """Tier 1: the MCP contextual axis is still carried-but-not-enforced — a
    ContextualPermission that DENIES an mcp value is ⊤ on MCP through
    ContextualLayer (S1 carries the axis; #2074 S4a wires MCP enforcement, paired
    with the require_mcp gate). (SKILL is now enforced by S3 — see
    test_2074_s3_contextual_skill.py.)"""
    ctx = ContextualPermission(
        mcp_allow=frozenset({"only-srv"}),
        mcp_deny=frozenset({"banned-srv"}),
    )
    layer = ContextualLayer(ctx)
    assert layer.allows(AX.MCP, "banned-srv") is True
    assert layer.allows(AX.MCP, "anything") is True


def test_contextual_layer_still_enforces_tool() -> None:
    """Tier 1: TOOL enforcement is unchanged by S1 (no regression on the live axis)."""
    layer = ContextualLayer(ContextualPermission(tool_deny=frozenset({"danger"})))
    assert layer.allows(AX.TOOL, "danger") is False
    assert layer.allows(AX.TOOL, "safe") is True
