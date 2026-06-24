"""Tier 2: #2111 — the capability FLOOR denies EVERY invocable form (bare + qualified).

CRITICAL security regression (tui live-probe): under delegation.capability_default=deny
a delegate called bare ``remember_shared`` → it executed → persisted to shared memory.
The floor's memory-write + mcp-install classes listed only the QUALIFIED catalog names
(``memory_operation__remember_shared``), missing the bare unwrapped aliases the live
gate actually receives. ONE root (the shared ``_FLOORED_DENY_CLASSES``), TWO surfaces:
both ``builtin_untrusted_profile`` (#1827, always-on while untrusted content is live —
the prompt-injection persistence surface) and ``builtin_delegate_profile`` (#2081).

Fix: the floored classes are defined by their qualified names + the bare aliases are
DERIVED from the invoke_action unwrap source-of-truth (``unwrapped_tool_name``) →
complete-by-construction.

Two guards:
- completeness-invariant: every floored tool's bare AND qualified form ∈ the floor,
  enumerated FROM the unwrap SoT (a future floored tool missing its alias → RED).
- live-gate falsify: a REAL ContextualPermission from each floor, through the REAL
  contextual gate seam (``tool_contextually_denied`` — the exact fn router_loop +
  op-runtime call) → each form DENIED under the floor, ALLOWED under inherit/no-floor.
  Drop a bare alias from the floor → the gate lets it through → RED (non-tautological).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.runtime.registry import AgentRegistry
from reyn.security.permissions.capability_profile import (
    _BUILTIN_UNTRUSTED_DENY,
    _FLOORED_QUALIFIED,
    builtin_untrusted_profile,
    resolve_profile,
)
from reyn.security.permissions.effective import tool_contextually_denied
from reyn.tools.universal_dispatch import unwrapped_tool_name


def _all_floored_forms() -> list[str]:
    """Every invocable form (qualified + bare unwrapped alias) of every floored tool,
    enumerated FROM the unwrap source-of-truth — the test's expectation is derived, not
    hand-listed, so a new floored tool is covered automatically."""
    forms: set[str] = set()
    for qualifieds in _FLOORED_QUALIFIED.values():
        for q in qualifieds:
            forms.add(q)
            bare = unwrapped_tool_name(q)
            if bare is not None:
                forms.add(bare)
    return sorted(forms)


# ── completeness-invariant (SoT-derived) ────────────────────────────────────


def test_every_floored_tool_has_both_forms_in_the_floor() -> None:
    """Tier 2: each floored name is in the floor — AND, for a name with an
    invoke_action route, its bare unwrapped alias too (the live gate receives the bare
    form; a missing alias → RED, the gap-class guard). A BARE-ONLY router tool (no
    qualified route, e.g. session_spawn) has no alias and is floored by its own name."""
    for cls, qualifieds in _FLOORED_QUALIFIED.items():
        for q in qualifieds:
            assert q in _BUILTIN_UNTRUSTED_DENY, f"{cls}: {q!r} missing from floor"
            bare = unwrapped_tool_name(q)
            if bare is not None:  # has an invoke_action route → its bare alias must floor too
                assert bare in _BUILTIN_UNTRUSTED_DENY, (
                    f"{cls}: bare alias {bare!r} of {q!r} missing from floor — the live "
                    "gate receives the bare form (#2111 regression)"
                )


def test_session_spawn_is_floored() -> None:
    """Tier 2: #2103 S1bc — session_spawn (a new spawning capability) is in the floor,
    so an unbound-delegate-under-deny / untrusted-content turn cannot spawn unbounded
    sub-sessions (DoS). (Live-gate denial across both floors is covered by the
    parametrized tests below, which enumerate session_spawn via _all_floored_forms.)"""
    assert "session_spawn" in _BUILTIN_UNTRUSTED_DENY


def test_bare_memory_write_aliases_present() -> None:
    """Tier 2: the exact tui-probe regression — bare memory-write aliases are denied
    (the form that slipped through and persisted to shared memory)."""
    assert {"remember_shared", "remember_agent", "forget_memory"} <= _BUILTIN_UNTRUSTED_DENY


# ── live-gate falsify: REAL ContextualPermission through the REAL gate seam ──


@pytest.mark.parametrize("tool", _all_floored_forms())
def test_untrusted_floor_denies_every_form_at_the_live_gate(tool: str) -> None:
    """Tier 2: the #1827 untrusted-content floor (auto-applied while untrusted content
    is live) DENIES every floored form (bare + qualified) at the real contextual gate.
    Drop a bare alias → the gate lets it through → RED."""
    contextual, _ = resolve_profile(builtin_untrusted_profile())
    assert tool_contextually_denied(contextual, tool), (
        f"untrusted floor does NOT deny {tool!r} at the live gate (#2111)"
    )


@pytest.mark.parametrize("tool", _all_floored_forms())
def test_delegate_floor_denies_every_form_via_real_resolution(tool: str, tmp_path: Path) -> None:
    """Tier 2: the #2081 delegate floor — through the REAL registry resolution path
    (resolved_profile_for(is_delegate=True) under deny → ContextualPermission) → the
    real gate seam DENIES every form. (Not a hand-built profile: the production path.)"""
    reg = AgentRegistry(
        project_root=tmp_path, session_factory=lambda p: None,
        delegation_capability_default="deny",
    )
    contextual, _ = reg.resolved_profile_for("worker", is_delegate=True)
    assert contextual is not None
    assert tool_contextually_denied(contextual, tool), (
        f"delegate floor does NOT deny {tool!r} at the live gate (#2111)"
    )


@pytest.mark.parametrize("tool", _all_floored_forms())
def test_inherit_allows_every_form(tool: str, tmp_path: Path) -> None:
    """Tier 2: regression — under capability_default=inherit (the default), an unbound
    delegate gets NO floor → the gate ALLOWS every form (the floor is what denies; a
    fix that over-denies under inherit would break byte-identical pre-#2081)."""
    reg = AgentRegistry(
        project_root=tmp_path, session_factory=lambda p: None,
        delegation_capability_default="inherit",
    )
    contextual, _ = reg.resolved_profile_for("worker", is_delegate=True)
    assert not tool_contextually_denied(contextual, tool)  # contextual is None → allowed
