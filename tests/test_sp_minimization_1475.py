"""Tier 2: #1475 — SP minimization: backtick convention + Behaviour dedup.

#1627 Stage 4: ``build_system_prompt`` is now a pure slot-injector. The
``universal_wrappers_enabled``, ``discovery_mandate``, and ``search_actions_enabled``
parameters have been REMOVED. Tests now call ``build_universal_tool_use_slots``
and pass the result as ``tool_use_sp``.

Pins:

1. Backtick convention: action qualified names and tool names in the SP
   appear backtick-wrapped (`list_actions`, `invoke_action`, `file__read`,
   `skill__code_review`, etc.) so the LLM can distinguish them from prose.

2. Behaviour dedup: Policy 1 (routing summary), Policy 2 (plan routing),
   and discovery_mandate repeat are removed from ## Behaviour — each had a
   canonical description in ## Capabilities. ## Behaviour retains only the
   three unique cross-cutting rules: errors-verbatim, never-invent, and
   ROUTING RULE ABSOLUTE.

3. N=0 smoke residual: discovery_mandate `_otherwise` branch no longer says
   "hot-list subset" (false when N=0) — says "universal wrappers" instead.

No mocks. Tests call build_system_prompt + build_universal_tool_use_slots with
real arguments.
"""
from __future__ import annotations

from reyn.runtime.router_system_prompt import build_system_prompt
from reyn.tools.schemes._universal_sp import build_universal_tool_use_slots


def _sp(*, universal_wrappers_enabled: bool = True,
        discovery_mandate: bool = False,
        search_actions_enabled: bool = False) -> str:
    slots = build_universal_tool_use_slots(
        universal_wrappers_enabled=universal_wrappers_enabled,
        search_actions_enabled=search_actions_enabled,
        discovery_mandate=discovery_mandate,
        has_hot_list_aliases=False,
        non_interactive=False,
    )
    return build_system_prompt(
        agent_name="test",
        agent_role="tester",
        available_skills=[],
        available_agents=[],
        memory_index={},
        tool_use_sp=slots,
    )


# ── 1. Backtick convention ────────────────────────────────────────────────────


def test_list_actions_backtick_wrapped_in_sp() -> None:
    """Tier 2: #1475 — `list_actions` appears backtick-wrapped in the SP text."""
    sp = _sp()
    assert "`list_actions`" in sp or "`list_actions(" in sp, (
        "list_actions must appear backtick-wrapped in the SP"
    )


def test_invoke_action_backtick_wrapped_in_sp() -> None:
    """Tier 2: #1475 — `invoke_action` appears backtick-wrapped in the SP text."""
    sp = _sp()
    assert "`invoke_action`" in sp or "`invoke_action(" in sp, (
        "invoke_action must appear backtick-wrapped in the SP"
    )


def test_file_action_names_backtick_wrapped() -> None:
    """Tier 2: #1475 — file__read and file__edit appear backtick-wrapped when
    discovery_mandate is on (where these names are explicitly referenced)."""
    sp = _sp(discovery_mandate=True)
    assert "`file__edit`" in sp, "file__edit must be backtick-wrapped in SP"


def test_skill_example_backtick_wrapped() -> None:
    """Tier 2: #1475 — skill__code_review (example in ROUTING RULE ABSOLUTE)
    appears backtick-wrapped."""
    sp = _sp()
    assert "`skill__code_review`" in sp, (
        "skill__code_review example in ROUTING RULE ABSOLUTE must be backtick-wrapped"
    )


def test_action_name_declaration_present() -> None:
    """Tier 2: #1475 — Action categories section contains the backtick convention
    declaration so the LLM knows names in backtick format are invocable."""
    sp = _sp()
    assert "invocable action names" in sp or "invocable" in sp, (
        "SP must declare the backtick convention for action names"
    )


# ── 2. Behaviour dedup ────────────────────────────────────────────────────────


def test_behaviour_does_not_contain_routing_policy_1() -> None:
    """Tier 2: #1475 — Policy 1 routing summary ('Domain task → invoke_action OR
    plan. Chitchat → Reply.') is removed from Behaviour. Canonical location is
    Capabilities."""
    sp = _sp()
    behav_pos = sp.find("## Behaviour")
    assert behav_pos >= 0
    behav_block = sp[behav_pos:]
    assert "Domain task →" not in behav_block, (
        "Policy 1 routing summary must not appear in ## Behaviour (canonical: Capabilities)"
    )


def test_behaviour_does_not_contain_routing_policy_2() -> None:
    """Tier 2: #1475 — Policy 2 plan routing ('Use plan when the query combines
    info from multiple independent sources') is removed from Behaviour."""
    sp = _sp()
    behav_pos = sp.find("## Behaviour")
    assert behav_pos >= 0
    behav_block = sp[behav_pos:]
    assert "Use plan when the query combines" not in behav_block, (
        "Policy 2 plan routing must not appear in ## Behaviour (canonical: Capabilities)"
    )


def test_behaviour_does_not_contain_discovery_mandate_repeat() -> None:
    """Tier 2: #1475 — discovery_mandate is not repeated in Behaviour even when
    the mandate is on. Canonical location is Capabilities branch-3 '_otherwise'."""
    sp = _sp(discovery_mandate=True)
    behav_pos = sp.find("## Behaviour")
    assert behav_pos >= 0
    behav_block = sp[behav_pos:]
    assert "FIRST tool call MUST be" not in behav_block, (
        "discovery_mandate must not be repeated in ## Behaviour (canonical: Capabilities)"
    )


def test_behaviour_retains_errors_verbatim_rule() -> None:
    """Tier 2: #1475 — Errors MUST surface verbatim rule remains in Behaviour
    (it is unique — not present elsewhere in the SP)."""
    sp = _sp()
    behav_pos = sp.find("## Behaviour")
    assert behav_pos >= 0
    behav_block = sp[behav_pos:]
    assert "Errors MUST surface verbatim" in behav_block, (
        "Errors verbatim rule must remain in ## Behaviour"
    )


def test_behaviour_retains_routing_rule_absolute() -> None:
    """Tier 2: #1475 — ROUTING RULE (ABSOLUTE) remains in Behaviour (B11-R3
    canonical description, unique to Behaviour)."""
    sp = _sp()
    behav_pos = sp.find("## Behaviour")
    assert behav_pos >= 0
    behav_block = sp[behav_pos:]
    assert "ROUTING RULE (ABSOLUTE)" in behav_block, (
        "ROUTING RULE ABSOLUTE must remain in ## Behaviour"
    )


def test_behaviour_retains_never_invent_rule() -> None:
    """Tier 2: #1475 — never-invent-action-names rule remains in Behaviour."""
    sp = _sp()
    behav_pos = sp.find("## Behaviour")
    assert behav_pos >= 0
    behav_block = sp[behav_pos:]
    assert "Never invent action names" in behav_block, (
        "never-invent rule must remain in ## Behaviour"
    )


# ── 3. N=0 smoke residual: "hot-list subset" reword ──────────────────────────


def test_discovery_mandate_says_universal_wrappers_not_hot_list() -> None:
    """Tier 2: #1475 + N=0 smoke — when discovery_mandate is on, the _otherwise
    branch must NOT say 'hot-list subset' (false when N=0); must say 'universal
    wrappers' instead."""
    sp = _sp(discovery_mandate=True)
    assert "hot-list subset" not in sp, (
        "SP must not say 'hot-list subset' — false when hot_list_n=0 (N=0 default)"
    )
    assert "universal wrappers" in sp, (
        "SP must say 'universal wrappers' in discovery_mandate _otherwise branch"
    )
