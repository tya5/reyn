"""Tier 1/2: #2074 S4a — MCP source migration + per-context MCP + identity factor.

S4a completes the MCP axis of the unification:
- **MCP source migration**: the per-agent MCP allowlist moves OUT of
  ``AgentLayer.MCP`` (now grant-only) INTO a ``ProfileLayer`` in ``require_mcp``
  (symmetric with SKILL). Byte-identical at the gate (∩ associative) — guarded by
  the existing require_mcp suite (test_permissions.py) + test_1199_s31b_mcp_cutover.
- **per-context MCP**: ``ContextualLayer`` now enforces the MCP axis, and
  ``require_mcp`` adds it (⊤-when-unset — byte-identical for any context that does
  not narrow MCP). Mirrors S3's SKILL.
- **identity factor**: ``AgentProfile.default_profile()`` exposes the canonical
  unified capability spec (maps the user-facing allowed_skills/allowed_mcp onto the
  internal skill_allow/mcp_allow; no user-facing rename).

No mocks: real ContextualPermission / ContextualLayer / PermissionResolver (via
the support factory) / AgentProfile / CapabilityProfile.
"""
from __future__ import annotations

import asyncio

import pytest

from reyn.runtime.profile import AgentProfile
from reyn.security.permissions.effective import (
    CapabilityAxis,
    ContextualLayer,
    ContextualPermission,
)
from reyn.security.permissions.permissions import PermissionDecl
from reyn.user_intervention import InterventionAnswer, InterventionBus, UserIntervention
from tests._support.permissions import make_resolver as _make_resolver

AX = CapabilityAxis


class _AutoDenyBus(InterventionBus):
    """Real auto-denying bus (the config-explicit tests never reach it)."""

    def __init__(self) -> None:
        self.requests: list[UserIntervention] = []

    async def request(self, iv: UserIntervention) -> InterventionAnswer:
        self.requests.append(iv)
        return InterventionAnswer(choice_id="no")


def _run(coro):
    return asyncio.run(coro)


# ── ContextualLayer now enforces the MCP axis ───────────────────────────────


def test_contextual_layer_mcp_deny() -> None:
    """Tier 1: a contextual mcp_deny narrows MCP through ContextualLayer."""
    layer = ContextualLayer(ContextualPermission(mcp_deny=frozenset({"banned-srv"})))
    assert layer.allows(AX.MCP, "banned-srv") is False
    assert layer.allows(AX.MCP, "other-srv") is True


def test_contextual_layer_mcp_allow() -> None:
    """Tier 1: a contextual mcp_allow restricts MCP to its members."""
    layer = ContextualLayer(ContextualPermission(mcp_allow=frozenset({"only-srv"})))
    assert layer.allows(AX.MCP, "only-srv") is True
    assert layer.allows(AX.MCP, "other-srv") is False


def test_contextual_layer_mcp_top_when_unset() -> None:
    """Tier 1: ⊤-when-unset — a context that does not narrow MCP allows all."""
    assert ContextualLayer(ContextualPermission()).allows(AX.MCP, "anything") is True
    assert ContextualLayer(None).allows(AX.MCP, "anything") is True


# ── require_mcp: per-context MCP enforcement (gate) ─────────────────────────


def test_require_mcp_contextual_deny_blocks(tmp_path) -> None:
    """Tier 2: a contextual mcp_deny refuses a server the per-agent layer grants,
    with the NEW decision-enabling 'active capability context' message (#2074 S4a)."""
    resolver = _make_resolver(tmp_path, config={"mcp.fs": "allow"})
    decl = PermissionDecl(mcp=["fs"], allowed_mcp=None)  # granted + no per-agent narrowing
    ctx = ContextualPermission(mcp_deny=frozenset({"fs"}))
    with pytest.raises(PermissionError, match="active capability context"):
        _run(resolver.require_mcp(decl, "fs", _AutoDenyBus(), contextual=ctx))


def test_require_mcp_contextual_none_unchanged(tmp_path) -> None:
    """Tier 2: contextual=None → byte-identical (a granted server passes)."""
    resolver = _make_resolver(tmp_path, config={"mcp.fs": "allow"})
    decl = PermissionDecl(mcp=["fs"], allowed_mcp=None)
    _run(resolver.require_mcp(decl, "fs", _AutoDenyBus(), contextual=None))  # no raise


def test_require_mcp_contextual_unset_is_top(tmp_path) -> None:
    """Tier 2: a context that does not narrow MCP is ⊤ — granted server still passes
    (the load-bearing ⊤-when-unset property)."""
    resolver = _make_resolver(tmp_path, config={"mcp.fs": "allow"})
    decl = PermissionDecl(mcp=["fs"], allowed_mcp=None)
    empty = ContextualPermission()  # no mcp narrowing
    _run(resolver.require_mcp(decl, "fs", _AutoDenyBus(), contextual=empty))  # no raise


def test_require_mcp_contextual_does_not_regrant_allowlist(tmp_path) -> None:
    """Tier 2: a permissive contextual cannot RE-GRANT a server the per-agent
    allowlist denies (∩ never-elevate). allowed_mcp=[] blocks 'fs' regardless of
    an empty/permissive context."""
    resolver = _make_resolver(tmp_path, config={"mcp.fs": "allow"})
    decl = PermissionDecl(mcp=["fs"], allowed_mcp=[])  # per-agent allowlist blocks all
    with pytest.raises(PermissionError, match="allowed_mcp"):
        _run(resolver.require_mcp(decl, "fs", _AutoDenyBus(), contextual=ContextualPermission()))


# ── identity factor: AgentProfile.default_profile() ─────────────────────────


def test_default_profile_maps_allowlists_to_spec() -> None:
    """Tier 1: default_profile() maps the user-facing allowed_skills/allowed_mcp
    onto the unified spec's skill_allow/mcp_allow (internal representation)."""
    prof = AgentProfile(name="a", allowed_skills=["s1", "s2"], allowed_mcp=["m1"])
    spec = prof.default_profile()
    assert spec.skill_allow == ("s1", "s2")
    assert spec.mcp_allow == ("m1",)
    assert spec.name == "a"


def test_default_profile_none_passthrough() -> None:
    """Tier 1: None allowlists pass through as None (= ⊤, unrestricted)."""
    spec = AgentProfile(name="a").default_profile()
    assert spec.skill_allow is None
    assert spec.mcp_allow is None


# ── FALSIFY NOTE (held-oracle ×3, run + reverted during S4a build) ──────────
# 1. MCP source migration byte-identical: break AgentLayer.MCP (re-add the
#    allowlist) or ProfileLayer MCP → test_permissions.py require_mcp suite +
#    test_1199_s31b_mcp_cutover (_mcp_gate matrix) go RED.
# 2. MCP contextual: break ContextualLayer.allows(MCP) (force ⊤) →
#    test_contextual_layer_mcp_deny/_allow + test_require_mcp_contextual_deny_blocks
#    go RED, while ⊤-when-unset + byte-identical tests stay GREEN.
# 3. Both deny messages preserved: test_permissions.py (allowed_mcp + not declared)
#    + test_require_mcp_contextual_deny_blocks (active capability context).
# Confirmed CLEAN red on each break, green on revert.
