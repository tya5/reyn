"""Tier 1: #2074 S4b — the per-agent ProfileLayer reads the unified capability spec.

S4b repoints the per-agent ∩ layer to read ``AgentProfile.default_profile()`` (a
``CapabilityProfile`` — the single unified primitive) on its ``skill_allow`` /
``mcp_allow`` axes, instead of the extracted ``allowed_skills`` / ``allowed_mcp``
values (or the old ``_AllowlistSource``). This completes the one-spec / two-binding
model: BOTH binding adapters (per-agent ProfileLayer + per-context ContextualLayer)
now read a ``CapabilityProfile``.

**Byte-identical**: ``default_profile().skill_allow == allowed_skills`` (same
values), so the read-source rewiring changes no outcome — proven by the
extracted-vs-spec identity below. The falsify is the spec-read itself: breaking
``ProfileLayer.allows`` (the spec membership) turns the per-agent SKILL/MCP gates
RED (the existing S2/S4a site + gate oracles).

No mocks: real CapabilityProfile / AgentProfile / ProfileLayer.
"""
from __future__ import annotations

import pytest

from reyn.runtime.profile import AgentProfile
from reyn.security.permissions.capability_profile import CapabilityProfile
from reyn.security.permissions.effective import CapabilityAxis, ProfileLayer

AX = CapabilityAxis


# ── ProfileLayer reads the spec's skill_allow / mcp_allow ───────────────────


def test_profile_layer_reads_spec_skill_axis() -> None:
    """Tier 1: ProfileLayer reads CapabilityProfile.skill_allow (None = ⊤)."""
    layer = ProfileLayer(CapabilityProfile(name="p", skill_allow=("a",)))
    assert layer.allows(AX.SKILL, "a") is True
    assert layer.allows(AX.SKILL, "b") is False
    # skill_allow None → unrestricted
    assert ProfileLayer(CapabilityProfile(name="p")).allows(AX.SKILL, "anything") is True
    # None spec → ⊤
    assert ProfileLayer(None).allows(AX.SKILL, "anything") is True


def test_profile_layer_reads_spec_mcp_axis() -> None:
    """Tier 1: ProfileLayer reads CapabilityProfile.mcp_allow (None = ⊤)."""
    layer = ProfileLayer(CapabilityProfile(name="p", mcp_allow=("srv",)))
    assert layer.allows(AX.MCP, "srv") is True
    assert layer.allows(AX.MCP, "other") is False
    assert ProfileLayer(CapabilityProfile(name="p")).allows(AX.MCP, "anything") is True


# ── default_profile() feeds the per-agent layer ─────────────────────────────


def test_default_profile_feeds_profile_layer() -> None:
    """Tier 1: ProfileLayer(agent.default_profile()) enforces the agent's
    allowed_skills/allowed_mcp via the spec's skill_allow/mcp_allow."""
    agent = AgentProfile(name="a", allowed_skills=["s1"], allowed_mcp=["m1"])
    layer = ProfileLayer(agent.default_profile())
    assert layer.allows(AX.SKILL, "s1") is True
    assert layer.allows(AX.SKILL, "s2") is False
    assert layer.allows(AX.MCP, "m1") is True
    assert layer.allows(AX.MCP, "m2") is False


# ── the read-source rewiring identity (the S4b falsify oracle) ──────────────


@pytest.mark.parametrize("allowed", [None, [], ["a"], ["a", "b"]])
@pytest.mark.parametrize("name", ["a", "b", "z"])
def test_spec_read_matches_extracted_skill(allowed, name) -> None:
    """Tier 1: reading the spec (default_profile().skill_allow) is byte-identical to
    the extracted-allowlist read (from_allowlists) on SKILL — so S4b's layer
    rewiring is byte-identical. Breaking ProfileLayer's spec-read flips BOTH."""
    via_spec = ProfileLayer(
        AgentProfile(name="_", allowed_skills=allowed).default_profile()
    ).allows(AX.SKILL, name)
    via_extracted = ProfileLayer.from_allowlists(allowed_skills=allowed).allows(AX.SKILL, name)
    assert via_spec is via_extracted


@pytest.mark.parametrize("allowed", [None, [], ["m1"], ["m1", "m2"]])
@pytest.mark.parametrize("name", ["m1", "m2", "z"])
def test_spec_read_matches_extracted_mcp(allowed, name) -> None:
    """Tier 1: same read-source identity on the MCP axis (require_mcp's ProfileLayer)."""
    via_spec = ProfileLayer(
        AgentProfile(name="_", allowed_mcp=allowed).default_profile()
    ).allows(AX.MCP, name)
    via_extracted = ProfileLayer.from_allowlists(allowed_mcp=allowed).allows(AX.MCP, name)
    assert via_spec is via_extracted
