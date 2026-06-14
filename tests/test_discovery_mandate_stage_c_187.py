"""Tier 2: #187 Stage C — weak-tier discovery mandate (composed into taxonomy).

owner decision: weak router tiers under-explore the catalog (satisfice instead
of discovering the action they need). Rather than a standalone unconditional
mandate (which would reverse B11-R3's named-skill→direct-invoke fix and
re-introduce the clarification-fallthrough attractor #187 fights), Stage C
STRENGTHENS the existing V18 intent taxonomy's branch-3 "Otherwise" routing hint
(soft → mechanical MUST), reinforced 3x (branch-3 / §D9 hot-list / Behaviour),
each carrying a NON-obvious/unknown/not-named scope qualifier. Tier-gated
(light ON / strong+unknown OFF).

Pinned invariants:

- ``tier_wants_discovery_mandate``: only the verified weak tier (``light``) opts
  in; ``strong`` / unknown / ``None`` stay OFF.
- When ``discovery_mandate=True``, all 3 reinforcements render, each
  scope-qualified + mechanical MUST/MANDATORY, with the verified
  explicit-action-enumeration "reading, writing, or editing" (NOT the generic
  "before acting", which detunes 25-55% → 0-10%). file__edit MUST is carried.
- When False, the SP keeps the SOFT branch-3 "Otherwise <chain>" + no MUST /
  MANDATORY reinforcement (byte-clean for non-weak tiers → valid replay
  fixtures, strong latitude preserved).
- B11-R3 (obvious/named → invoke directly) + the Conversation (branch-1) /
  Question (branch-2) wording are UNTOUCHED in BOTH modes (diff-touch-0): the
  scope qualifier is structural, so chitchat / named-skill / direct routing are
  preserved by construction.
- All reinforcements sit in the static cacheable prefix (before project_context).
- router_loop wires the call site with ``tier_wants_discovery_mandate``.

The live behavioural proof (weak model fires list_actions-first ~75-85% on
genuine unnamed-discovery WITHOUT bleeding chitchat/named into discovery) is
sandbox_2's production-flow verify, not a unit test.
"""
from __future__ import annotations

import ast
from pathlib import Path

import reyn.chat.router_loop as rl_mod
from reyn.chat.router_system_prompt import (
    build_system_prompt,
)
from reyn.tools.schemes._discovery import tier_wants_discovery_mandate

_BASE = dict(
    agent_name="chat",
    agent_role="test role",
    available_skills=[],
    available_agents=[],
    memory_index={"status": "not_found", "content": ""},
    universal_wrappers_enabled=True,
)

# Markers for the B11-R3 named-direct clause + branch-1 Conversation, which must
# be byte-stable across discovery_mandate on/off (diff-touch-0).
_B11R3_MARKER = "named skill), invoke directly"
_CONVERSATION_MARKER = "reply"


def _on() -> str:
    return build_system_prompt(**_BASE, discovery_mandate=True)


def _off() -> str:
    return build_system_prompt(**_BASE, discovery_mandate=False)


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
    on = build_system_prompt(**_BASE, project_context=marker, discovery_mandate=True)
    assert marker in on
    # The canonical reinforcement (branch-3) must come before the dynamic marker.
    assert on.rindex("FIRST tool call MUST be `list_actions`") < on.index(marker)


# ---------------------------------------------------------------------------
# Call-site wiring (AST) — router_loop gates on the tier
# ---------------------------------------------------------------------------


def test_router_loop_wires_discovery_mandate_gate() -> None:
    """Tier 2: #187 Stage C — router_loop's build_system_prompt(...) call passes
    ``discovery_mandate=tier_wants_discovery_mandate(...)``. Falsifiable: drop the
    gate (or hardcode True/False) → this fails, naming the construction-wiring
    gap. Without the gate the mandate is either unreachable or applied to every
    tier (strong included)."""
    tree = ast.parse(Path(rl_mod.__file__).read_text(encoding="utf-8"))
    wired = False
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "build_system_prompt"
        ):
            continue
        for kw in node.keywords:
            if kw.arg != "discovery_mandate":
                continue
            v = kw.value
            if (
                isinstance(v, ast.Call)
                and isinstance(v.func, ast.Name)
                and v.func.id == "tier_wants_discovery_mandate"
            ):
                wired = True
    assert wired, (
        "router_loop must call build_system_prompt(..., "
        "discovery_mandate=tier_wants_discovery_mandate(self.router_model))"
    )
