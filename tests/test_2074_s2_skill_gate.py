"""Tier 1: #2074 S2 — the per-agent SKILL allowlist routes onto the live ∩.

S2 replaces the three inline ``allowed_skills`` checks (skill_runner spawn gate A
+ B, router-host catalog filter) with one shared decision — ``skill_allowed`` →
``EffectivePermission([ProfileLayer])`` — completing #1199's ∩ convergence for the
SKILL axis. The change is **byte-identical**: ``None`` = unrestricted, ``[]`` =
none allowed, ``[a,b]`` = only those; ``skill_router`` stays exempt (excluded
upstream, never a spawn target).

Byte-identical regression for the 3 sites lives in the existing allowlist tests
(test_skill_runner_pre_spawn_validation.py + the list_available_skills tests),
which still pass unchanged. THIS file pins the new shared seam + the falsify
linkage: ``skill_allowed`` IS the ``ProfileLayer`` SKILL decision, so breaking
``ProfileLayer.allows(SKILL)`` breaks every routed site (held-oracle ×3 — see the
falsify note at the bottom).

No mocks: real ProfileLayer / EffectivePermission / AgentProfile + the pure
``skill_allowed`` seam.
"""
from __future__ import annotations

import asyncio

import pytest
from test_skill_runner_invariants import _make_runner

from reyn.runtime.profile import AgentProfile
from reyn.runtime.services.router_host_adapter import RouterHostAdapter
from reyn.security.permissions.effective import (
    CapabilityAxis,
    EffectivePermission,
    ProfileLayer,
    skill_allowed,
)

AX = CapabilityAxis


# ── the seam's byte-identical allowlist matrix ──────────────────────────────


@pytest.mark.parametrize(
    "allowed, name, expected",
    [
        (None, "anything", True),       # None = unrestricted (⊤)
        ([], "anything", False),        # [] = nothing allowed
        (["a", "b"], "a", True),        # listed → allowed
        (["a", "b"], "z", False),       # not listed → refused
    ],
)
def test_skill_allowed_matrix(allowed, name, expected) -> None:
    """Tier 1: skill_allowed is byte-identical to the legacy ``allowed is None or
    name in allowed`` across the None / [] / [a,b] matrix."""
    assert skill_allowed(allowed, name) is expected


# ── falsify linkage: skill_allowed IS the ProfileLayer ∩ decision ───────────


@pytest.mark.parametrize("allowed", [None, [], ["a"], ["a", "b"]])
@pytest.mark.parametrize("name", ["a", "b", "z"])
def test_skill_allowed_is_the_profilelayer_decision(allowed, name) -> None:
    """Tier 1: skill_allowed delegates to EffectivePermission([ProfileLayer]) on the
    SKILL axis — the single decision the 3 routed sites share. So breaking
    ``ProfileLayer.allows(SKILL)`` changes this AND every routed site together
    (the held-oracle linkage that makes the falsify ×3 bite)."""
    via_layer = EffectivePermission(
        [ProfileLayer.from_allowlists(allowed_skills=allowed)]
    ).allows(AX.SKILL, name)
    assert skill_allowed(allowed, name) is via_layer


@pytest.mark.parametrize("allowed", [None, [], ["a"], ["a", "b"]])
@pytest.mark.parametrize("name", ["a", "b", "z"])
def test_from_allowlists_matches_agentprofile_source(allowed, name) -> None:
    """Tier 1: ProfileLayer.from_allowlists(allowed_skills=L) is byte-identical to
    ProfileLayer(AgentProfile(allowed_skills=L)) on the SKILL axis — the S2 factory
    introduces no semantic drift from the existing AgentProfile-backed layer."""
    from_factory = ProfileLayer.from_allowlists(allowed_skills=allowed).allows(AX.SKILL, name)
    from_profile = ProfileLayer(
        AgentProfile(name="_", allowed_skills=allowed)
    ).allows(AX.SKILL, name)
    assert from_factory is from_profile


def test_skill_allowed_does_not_constrain_other_axes() -> None:
    """Tier 1: the per-agent layer from from_allowlists(allowed_skills=...) is ⊤ on
    non-SKILL axes (S2 touches SKILL only; MCP stays on AgentLayer)."""
    layer = ProfileLayer.from_allowlists(allowed_skills=[])  # narrow SKILL to none
    assert layer.allows(AX.MCP, "any-server") is True
    assert layer.allows(AX.TOOL, "any-tool") is True


# ── held-oracle ×3: each routed SITE enforces via the seam (real instances) ──


def test_site1_spawn_gate_refuses_disallowed_skill() -> None:
    """Tier 2: spawn gate A (SkillRunner.spawn) — with allowed_skills set, spawning
    an un-listed skill is refused (returns None + skill_spawn_refused) via the
    routed seam. Falsifiable: break ProfileLayer.allows(SKILL) → 'denied' becomes
    allowed → not refused → RED."""
    runner, events, _outbox, _completed = _make_runner(allowed_skills=["allowed_one"])

    async def _run():
        result = await runner.spawn({"skill": "denied_one", "input": {"x": 1}})
        assert result is None  # allowlist gate refuses before load
        assert "skill_spawn_refused" in [e.type for e in events.all()]

    asyncio.run(_run())


def test_site2_run_skill_awaitable_refuses_disallowed_skill() -> None:
    """Tier 2: spawn gate B (SkillRunner.run_skill_awaitable) — same routed seam;
    a disallowed skill returns the allowlist error dict + event. Falsifiable as
    site 1."""
    runner, events, _outbox, _completed = _make_runner(allowed_skills=["allowed_one"])

    async def _run():
        result = await runner.run_skill_awaitable(
            {"skill": "denied_one", "input": {"x": 1}}, chain_id="c1"
        )
        assert result["status"] == "error"
        assert "not in allowed_skills" in result["data"]["error"]
        assert "skill_spawn_refused" in [e.type for e in events.all()]

    asyncio.run(_run())


def _catalog_host(allowed_skills, enumerated):
    """A real RouterHostAdapter exercising only list_available_skills via its two
    real inputs (the _skill_enumerate_fn DI seam + _allowed_skills) — no collaborator
    mocks, no 30-arg construction. ``enumerated`` is what enumerate returns (the
    router is already excluded upstream by the {skill_router} exclude arg)."""
    host = RouterHostAdapter.__new__(RouterHostAdapter)
    host._allowed_skills = allowed_skills
    host._contextual_permission = None  # #2074 S3: no per-context narrowing here
    host._skill_enumerate_fn = lambda exclude: list(enumerated)
    return host


def test_site3_catalog_filter_routes_through_seam() -> None:
    """Tier 2: catalog filter (RouterHostAdapter.list_available_skills) — visibility
    shares the routed seam; an un-listed skill is filtered out, preserving the
    visibility⇔spawn coupling. Falsifiable: break the seam → 'drop' kept / 'keep'
    dropped → RED."""
    host = _catalog_host(["keep"], [{"name": "keep"}, {"name": "drop"}])
    assert [s["name"] for s in host.list_available_skills()] == ["keep"]


def test_site3_catalog_none_is_unrestricted() -> None:
    """Tier 2: allowed_skills=None → no catalog filtering (byte-identical to the
    legacy `if self._allowed_skills is not None` guard)."""
    host = _catalog_host(None, [{"name": "a"}, {"name": "b"}])
    assert [s["name"] for s in host.list_available_skills()] == ["a", "b"]


# ── FALSIFY NOTE (held-oracle ×3, run + reverted during S2 build) ───────────
# Breaking the new path `ProfileLayer.allows(SKILL)` (e.g. invert the membership
# to `value not in pr.allowed_skills`) turns all routed enforcement RED:
#   - test_skill_allowed_matrix + the linkage tests above, AND
#   - the 3 routed sites' existing tests:
#       spawn gate A/B → tests/test_skill_runner_pre_spawn_validation.py
#       catalog filter → the list_available_skills allowlist tests
# Confirmed CLEAN red on the break, green on revert (byte-identical + falsifiable).
