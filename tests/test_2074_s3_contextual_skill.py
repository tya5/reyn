"""Tier 1/2: #2074 S3 — per-context SKILL narrowing onto the unified ∩.

S3 wires the contextual binding adapter for the SKILL axis: ContextualLayer now
consumes the S1 ``skill_allow`` / ``skill_deny`` axes, and ``skill_allowed`` adds
``ContextualLayer`` to its gate → ``EffectivePermission([ProfileLayer,
ContextualLayer])``. Both spawn gates + the catalog filter pass the session's
contextual_permission.

**Byte-identical discipline (⊤-when-unset):** contextual SKILL narrowing is NEW
capability, so it must be ⊤ whenever no bound context narrows SKILL —
``contextual=None`` OR a context with ``skill_allow=None`` + empty ``skill_deny``.
Then every pre-S3 (per-agent) outcome is unchanged; the existing S2 tests
(test_2074_s2_skill_gate.py) still pass because they call skill_allowed without a
contextual (= None = ⊤). New: a bound context that sets skill_allow/deny now
narrows spawn + catalog.

No mocks: real ContextualPermission / ContextualLayer / EffectivePermission +
real SkillRunner (via _make_runner) + real list_available_skills.
"""
from __future__ import annotations

import asyncio

from test_skill_runner_invariants import _make_runner

from reyn.runtime.services.router_host_adapter import RouterHostAdapter
from reyn.security.permissions.effective import (
    CapabilityAxis,
    ContextualLayer,
    ContextualPermission,
    skill_allowed,
)

AX = CapabilityAxis


# ── ContextualLayer now enforces the SKILL axis ─────────────────────────────


def test_contextual_layer_skill_deny() -> None:
    """Tier 1: a contextual skill_deny narrows SKILL through ContextualLayer."""
    layer = ContextualLayer(ContextualPermission(skill_deny=frozenset({"banned"})))
    assert layer.allows(AX.SKILL, "banned") is False
    assert layer.allows(AX.SKILL, "other") is True


def test_contextual_layer_skill_allow() -> None:
    """Tier 1: a contextual skill_allow restricts SKILL to its members."""
    layer = ContextualLayer(ContextualPermission(skill_allow=frozenset({"only"})))
    assert layer.allows(AX.SKILL, "only") is True
    assert layer.allows(AX.SKILL, "other") is False


def test_contextual_layer_skill_top_when_unset() -> None:
    """Tier 1: ⊤-when-unset — a context that does not narrow SKILL allows all."""
    assert ContextualLayer(ContextualPermission()).allows(AX.SKILL, "anything") is True
    assert ContextualLayer(None).allows(AX.SKILL, "anything") is True


# ── skill_allowed gate composes per-agent ∩ per-context ─────────────────────


def test_skill_allowed_contextual_none_matches_per_agent() -> None:
    """Tier 1: contextual=None → the decision matches the S2 per-agent-only result
    exactly (the contextual layer is ⊤ when absent — a permanent property)."""
    assert skill_allowed(["a"], "a", contextual=None) is True
    assert skill_allowed(["a"], "z", contextual=None) is False
    assert skill_allowed(None, "anything", contextual=None) is True


def test_skill_allowed_contextual_unset_is_top() -> None:
    """Tier 1: a context that does not narrow SKILL is ⊤ — per-agent outcome
    unchanged (the load-bearing byte-identical property)."""
    empty = ContextualPermission()  # no skill narrowing
    assert skill_allowed(None, "anything", contextual=empty) is True
    assert skill_allowed(["a"], "a", contextual=empty) is True
    assert skill_allowed(["a"], "z", contextual=empty) is False  # per-agent still applies


def test_skill_allowed_contextual_deny_narrows_even_when_agent_allows() -> None:
    """Tier 1: a contextual deny refuses a skill the per-agent layer allows
    (∩ never-elevate: the most-restrictive layer wins)."""
    ctx = ContextualPermission(skill_deny=frozenset({"dangerous"}))
    # per-agent unrestricted (None) but contextual denies
    assert skill_allowed(None, "dangerous", contextual=ctx) is False
    assert skill_allowed(None, "safe", contextual=ctx) is True


# ── site oracles: the spawn gate + catalog honor contextual SKILL narrowing ──


def test_spawn_gate_honors_contextual_skill_deny() -> None:
    """Tier 2: a SkillRunner with a contextual skill_deny refuses spawning the
    denied skill even when the per-agent allowlist is unrestricted (None)."""
    runner, events, _outbox, _completed = _make_runner(allowed_skills=None)
    runner._contextual_permission = ContextualPermission(skill_deny=frozenset({"denied_ctx"}))

    async def _run():
        result = await runner.spawn({"skill": "denied_ctx", "input": {"x": 1}})
        assert result is None
        assert "skill_spawn_refused" in [e.type for e in events.all()]

    asyncio.run(_run())


def _catalog_host(allowed_skills, contextual, enumerated):
    host = RouterHostAdapter.__new__(RouterHostAdapter)
    host._allowed_skills = allowed_skills
    host._contextual_permission = contextual
    host._skill_enumerate_fn = lambda exclude: list(enumerated)
    return host


def test_catalog_honors_contextual_skill_deny() -> None:
    """Tier 2: the catalog filter hides a contextually-denied skill (visibility
    ⇔ spawn coupling preserved across the contextual layer)."""
    ctx = ContextualPermission(skill_deny=frozenset({"hide_me"}))
    host = _catalog_host(None, ctx, [{"name": "keep"}, {"name": "hide_me"}])
    assert [s["name"] for s in host.list_available_skills()] == ["keep"]


def test_catalog_no_contextual_is_unrestricted() -> None:
    """Tier 2: no contextual (None) + no per-agent allowlist → no filtering
    (byte-identical to pre-S3)."""
    host = _catalog_host(None, None, [{"name": "a"}, {"name": "b"}])
    assert [s["name"] for s in host.list_available_skills()] == ["a", "b"]


# ── FALSIFY NOTE (held-oracle, run + reverted during S3 build) ──────────────
# Breaking the new path `ContextualLayer.allows(SKILL)` (e.g. drop the SKILL
# branch so it falls through to ⊤) turns the contextual-deny enforcement RED:
#   - test_contextual_layer_skill_deny / _skill_allow
#   - test_skill_allowed_contextual_deny_narrows_even_when_agent_allows
#   - test_spawn_gate_honors_contextual_skill_deny + test_catalog_honors_contextual_skill_deny
# while the ⊤-when-unset tests + all S2 (contextual=None) tests stay GREEN —
# proving the new enforcement is real AND byte-identical when unset. Confirmed
# CLEAN red on the break, green on revert.
