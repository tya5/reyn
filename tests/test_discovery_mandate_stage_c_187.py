"""Tier 2: #187 Stage C — weak-tier discovery mandate (composed into taxonomy).

#1627 Stage 4: ``build_system_prompt`` is now a pure slot-injector. The
``discovery_mandate`` parameter has been REMOVED from ``build_system_prompt`` and
moved fully into the scheme layer (``build_universal_tool_use_slots`` / schemes).
Tests that previously called ``build_system_prompt(..., discovery_mandate=True)``
now call ``build_universal_tool_use_slots`` directly and pass the result as
``tool_use_sp``. The AST wiring test is updated to verify that
``tier_wants_discovery_mandate`` is called in the scheme layer (universal_category.py)
rather than in router_loop.py's ``build_system_prompt`` call.

Pinned invariants:

- ``tier_wants_discovery_mandate``: only the verified weak tier (``light``) opts
  in; ``strong`` / unknown / ``None`` stay OFF.
- When ``discovery_mandate=True`` in ``build_universal_tool_use_slots``, all 3
  reinforcements render, each scope-qualified + mechanical MUST/MANDATORY.
- When False, the SP keeps the SOFT branch-3 "Otherwise <chain>" + no MUST /
  MANDATORY reinforcement.
- B11-R3 + Conversation/Question wording untouched in BOTH modes (diff-touch-0).
- router_loop wires the call through scheme layer (not build_system_prompt).
"""
from __future__ import annotations

import ast
from pathlib import Path

import reyn.tools.schemes.universal_category as uc_mod
from reyn.chat.router_system_prompt import build_system_prompt
from reyn.tools.schemes._discovery import tier_wants_discovery_mandate
from reyn.tools.schemes._universal_sp import build_universal_tool_use_slots

_BASE = dict(
    agent_name="chat",
    agent_role="test role",
    available_skills=[],
    available_agents=[],
    memory_index={"status": "not_found", "content": ""},
)

# Markers for the B11-R3 named-direct clause + branch-1 Conversation, which must
# be byte-stable across discovery_mandate on/off (diff-touch-0).
_B11R3_MARKER = "named skill), invoke directly"
_CONVERSATION_MARKER = "reply"


def _on() -> str:
    slots = build_universal_tool_use_slots(
        universal_wrappers_enabled=True,
        search_actions_enabled=True,
        discovery_mandate=True,
        has_hot_list_aliases=False,
        non_interactive=False,
    )
    return build_system_prompt(**_BASE, tool_use_sp=slots)


def _off() -> str:
    slots = build_universal_tool_use_slots(
        universal_wrappers_enabled=True,
        search_actions_enabled=True,
        discovery_mandate=False,
        has_hot_list_aliases=False,
        non_interactive=False,
    )
    return build_system_prompt(**_BASE, tool_use_sp=slots)


# ---------------------------------------------------------------------------
# Tier gate
# ---------------------------------------------------------------------------


def test_tier_gate_light_on_strong_unknown_off() -> None:
    """Tier 2: #187 Stage C — only the verified weak tier (light) opts in; strong,
    unknown, and None stay OFF (strong-flexibility-preserving default)."""
    assert tier_wants_discovery_mandate("light") is True
    assert tier_wants_discovery_mandate("strong") is False
    assert tier_wants_discovery_mandate("some_future_tier") is False
    assert tier_wants_discovery_mandate(None) is False


# ---------------------------------------------------------------------------
# 3x reinforcement when enabled — each scope-qualified + mechanical
# ---------------------------------------------------------------------------


def test_three_scope_qualified_reinforcements_when_enabled() -> None:
    """Tier 2: #187 Stage C — discovery_mandate renders with scope qualifier in
    Capabilities (canonical location). Behaviour dedup: mandate NOT repeated there.

    #1475: Policy 1/2 and discovery_mandate repeat removed from Behaviour (single
    canonical location in branch-3 + §D9). Pins the new single-canonical design:
    mandate present in Capabilities, absent from Behaviour.
    """
    on = _on()
    # ① branch-3 Otherwise (strengthened) — scope: "NOT obvious or a named skill"
    assert "for any action that is NOT obvious or a named skill above" in on
    # ② §D9 hot-list — scope: "no visible tool obviously matches"
    assert "When no visible tool obviously matches the action you need" in on
    assert "MANDATORY and comes FIRST" in on
    # ③ Behaviour dedup (#1475): mandate NOT repeated in Behaviour
    behav_pos = on.find("## Behaviour")
    assert behav_pos >= 0
    assert "FIRST tool call MUST be" not in on[behav_pos:], (
        "discovery_mandate must not be repeated in ## Behaviour (canonical: Capabilities)"
    )
    # Exactly 1 canonical MUST occurrence (branch-3); backtick-wrapped list_actions
    assert "`list_actions`" in on
    assert on.count("FIRST tool call MUST be `list_actions`") == 1
    # file__edit MUST lever (satisficing-counter) carried in ①
    assert "`file__edit`" in on


def test_explicit_action_enumeration_not_generic() -> None:
    """Tier 2: #187 Stage C — the verified explicit-action-enumeration
    ("reading, writing, or editing") is used, NOT the generic "before acting"
    that detunes the fire-rate (25-55% → 0-10%, the discovery-fire pin)."""
    on = _on()
    assert "reading, writing, or editing" in on
    assert "before acting" not in on
    assert "before any other tool" not in on


# ---------------------------------------------------------------------------
# Disabled — soft branch-3, no MUST/MANDATORY, byte-clean
# ---------------------------------------------------------------------------


def test_disabled_keeps_soft_branch3_no_mandate() -> None:
    """Tier 2: #187 Stage C — non-weak tiers keep the SOFT branch-3 "Otherwise"
    routing hint and get no MUST/MANDATORY reinforcement (byte-clean SP → valid
    replay fixtures, strong latitude preserved)."""
    off = _off()
    # The soft Otherwise + wrapper chain stays; no mechanical strengthening.
    assert "invoke directly. Otherwise" in off
    assert "FIRST tool call MUST be `list_actions`" not in off
    assert "MANDATORY and comes FIRST" not in off


# ---------------------------------------------------------------------------
# B11-R3 + branch-1/2 untouched (diff-touch-0) in BOTH modes
# ---------------------------------------------------------------------------


def test_b11r3_and_conversation_preserved_both_modes() -> None:
    """Tier 2: #187 Stage C — the B11-R3 obvious/named→invoke-directly clause and
    the Conversation branch are present in BOTH on/off (the strengthen touches
    only the Otherwise tail + appends §D9/Behaviour reinforcements; it never
    edits the obvious-clause or branch-1/2). This is the structural protection
    that preserves named-skill / chitchat routing by construction."""
    on, off = _on(), _off()
    assert _B11R3_MARKER in on and _B11R3_MARKER in off
    assert _CONVERSATION_MARKER in on and _CONVERSATION_MARKER in off


# ---------------------------------------------------------------------------
# Static cacheable prefix placement
# ---------------------------------------------------------------------------


def test_reinforcements_in_static_cacheable_prefix() -> None:
    """Tier 2: #187 Stage C — the canonical reinforcement (branch-3 Capabilities)
    precedes the dynamic ``project_context`` section (warm prompt cache preserved).

    #1475: Behaviour repeat removed; canonical location is branch-3. Pin updated
    to the single canonical MUST occurrence (backtick-wrapped `list_actions`).
    """
    marker = "ZZZ_DYNAMIC_PROJECT_CONTEXT_MARKER_ZZZ"
    slots = build_universal_tool_use_slots(
        universal_wrappers_enabled=True,
        search_actions_enabled=True,
        discovery_mandate=True,
        has_hot_list_aliases=False,
        non_interactive=False,
    )
    on = build_system_prompt(**_BASE, project_context=marker, tool_use_sp=slots)
    assert marker in on
    # The canonical reinforcement (branch-3) must come before the dynamic marker.
    assert on.rindex("FIRST tool call MUST be `list_actions`") < on.index(marker)


# ---------------------------------------------------------------------------
# Call-site wiring (AST) — scheme layer gates on the tier
# ---------------------------------------------------------------------------


def test_scheme_layer_wires_discovery_mandate_gate() -> None:
    """Tier 2: #187 Stage C / #1627 Stage 4 — ``tier_wants_discovery_mandate`` is
    called in the scheme layer (universal_category.py's build_presentation), NOT in
    router_loop.py's build_system_prompt call. Falsifiable: remove the call from
    universal_category → this fails, naming the construction-wiring gap.

    #1627 Stage 4 migration: the mandate computation moved out of router_loop (OS)
    into the scheme layer (P7). The AST check now targets universal_category.py."""
    tree = ast.parse(Path(uc_mod.__file__).read_text(encoding="utf-8"))
    wired = False
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "tier_wants_discovery_mandate"
        ):
            continue
        wired = True
        break
    assert wired, (
        "universal_category.build_presentation must call tier_wants_discovery_mandate "
        "to derive the discovery_mandate for build_universal_tool_use_slots "
        "(#1627 Stage 4: mandate computation relocated to scheme layer)"
    )
