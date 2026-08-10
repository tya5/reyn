"""Tier 2: #2111/#3429 — the capability FLOOR denies every floored tool, by name.

CRITICAL security regression (tui live-probe): under delegation.capability_default=deny
a delegate called bare ``remember_shared`` → it executed → persisted to shared memory.
The floor's memory-write + mcp-install classes listed only the QUALIFIED catalog names
(``memory_operation__remember_shared``), missing the bare unwrapped aliases the live
gate actually receives. ONE root (the shared ``_FLOORED_DENY_CLASSES``), TWO surfaces:
both ``builtin_untrusted_profile`` (#1827, applied while untrusted content is live
IF the operator opted in via ``safety.threat_scan.capability_narrowing`` — #3501 —
the prompt-injection persistence surface) and ``builtin_delegate_profile`` (#2081).

#2111's fix was to DERIVE the bare alias from the invoke_action unwrap
source-of-truth, so the two forms could not fall out of lockstep. #3429 removed
the second form outright: a tool has ONE invocable name, so the floor entry IS
the gated name and there is nothing left to derive. The derivation's job passes
to a different guard — every floored name must be a REGISTERED tool name, so a
typo floors nothing and is caught, rather than silently guarding a non-existent
form while the real route stays open (the #2111 gap-class, in the opposite
direction).

Two guards:
- completeness-invariant: every floored name is in the floor AND is a live
  registered tool (a typo → RED).
- live-gate falsify: a REAL ContextualPermission from each floor, through the REAL
  contextual gate seam (``tool_contextually_denied`` — the exact fn router_loop +
  op-runtime call) → each name DENIED under the floor, ALLOWED under inherit/no-floor.
  Drop a name from the floor → the gate lets it through → RED (non-tautological).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.runtime.registry import AgentRegistry
from reyn.security.permissions.capability_profile import (
    _BUILTIN_UNTRUSTED_DENY,
    _FLOORED_TOOLS,
    builtin_untrusted_profile,
    resolve_profile,
)
from reyn.security.permissions.effective import tool_contextually_denied
from reyn.tools import get_default_registry


def _all_floored_forms() -> list[str]:
    """Every floored tool name, enumerated FROM the floor table itself — the
    test's expectation is derived, not hand-listed, so a new floored tool is
    covered automatically."""
    forms: set[str] = set()
    for names in _FLOORED_TOOLS.values():
        forms |= set(names)
    return sorted(forms)


# ── completeness-invariant (SoT-derived) ────────────────────────────────────


def test_no_floor_class_is_empty() -> None:
    """Tier 2: vacuity guard — an emptied floor class (``frozenset()``) would
    pass ``test_every_floored_name_is_in_the_floor`` vacuously (nothing to
    check) while denying nothing, silently disabling that class's whole
    threat coverage. Same shape as #4110's chain-field vacuity guard.

    Named ahead of need: proposal 0067 P6 (#3978) retires ``delegate_to_agent``,
    the ONLY member ``re-delegation`` had before this PR added
    ``send_to_session`` — without that addition (architect ruling, #3978:
    send_to_session/run_prompt reach another agent's context the same way
    delegate_to_agent does), P6 would leave ``re-delegation`` empty and this
    test would have caught it before merge."""
    for cls, names in _FLOORED_TOOLS.items():
        assert names, f"{cls}: floor class is empty — denies nothing, looks like a guard"


def test_every_floored_name_is_in_the_floor() -> None:
    """Tier 2: each name declared in a floor class reaches the flat deny union
    the two builtin profiles are built from."""
    for cls, names in _FLOORED_TOOLS.items():
        for name in names:
            assert name in _BUILTIN_UNTRUSTED_DENY, f"{cls}: {name!r} missing from floor"


def test_every_floored_name_is_a_registered_tool() -> None:
    """Tier 2: #2111's gap-class in the opposite direction, re-guarded for #3429.

    A floored name that no tool answers to guards nothing — the floor LOOKS
    complete while the real route stays open. #2111 caught this by requiring
    every entry to unwrap through the alias table; with the alias table gone,
    the equivalent (and stricter) check is that the name is live in the
    registry."""
    registered = {tool.name for tool in get_default_registry()}
    unknown = [
        (cls, name)
        for cls, names in _FLOORED_TOOLS.items()
        for name in names
        if name not in registered
    ]
    assert unknown == [], (
        f"floored name(s) that are not registered tools — these deny nothing "
        f"and leave the real route unguarded: {sorted(unknown)!r}"
    )


def test_session_spawn_is_floored() -> None:
    """Tier 2: #2103 S1bc — spawn_session (a new spawning capability) is in the floor,
    so an unbound-delegate-under-deny / untrusted-content turn cannot spawn unbounded
    sub-sessions (DoS). (Live-gate denial across both floors is covered by the
    parametrized tests below, which enumerate spawn_session via _all_floored_forms.)"""
    assert "spawn_session" in _BUILTIN_UNTRUSTED_DENY


def test_bare_memory_write_aliases_present() -> None:
    """Tier 2: the exact tui-probe regression — the memory-write tools are denied
    (the form that slipped through and persisted to shared memory)."""
    assert {"remember_shared", "remember_agent", "forget_memory"} <= _BUILTIN_UNTRUSTED_DENY


# ── live-gate falsify: REAL ContextualPermission through the REAL gate seam ──


@pytest.mark.parametrize("tool", _all_floored_forms())
def test_untrusted_floor_denies_every_form_at_the_live_gate(tool: str) -> None:
    """Tier 2: the #1827 untrusted-content floor (applied while untrusted content
    is live) DENIES every floored tool at the real contextual gate.
    Drop a name from the floor → the gate lets it through → RED."""
    contextual, _ = resolve_profile(builtin_untrusted_profile())
    assert tool_contextually_denied(contextual, tool), (
        f"untrusted floor does NOT deny {tool!r} at the live gate (#2111)"
    )


@pytest.mark.parametrize("tool", _all_floored_forms())
def test_delegate_floor_denies_every_form_via_real_resolution(tool: str, tmp_path: Path) -> None:
    """Tier 2: the #2081 delegate floor — through the REAL registry resolution path
    (resolved_profile_for(is_delegate=True) under deny → ContextualPermission) → the
    real gate seam DENIES every floored tool. (Not a hand-built profile: the
    production path.)"""
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
    delegate gets NO floor → the gate ALLOWS every name (the floor is what denies; a
    fix that over-denies under inherit would break byte-identical pre-#2081)."""
    reg = AgentRegistry(
        project_root=tmp_path, session_factory=lambda p: None,
        delegation_capability_default="inherit",
    )
    contextual, _ = reg.resolved_profile_for("worker", is_delegate=True)
    assert not tool_contextually_denied(contextual, tool)  # contextual is None → allowed
