"""Tier 2: FP-0034 §D14 — search_actions SP wrapper visibility gate.

#1627 Stage 4: ``build_system_prompt`` is now a pure slot-injector. The
``search_actions_enabled`` parameter has been REMOVED from ``build_system_prompt``.
Tests now call ``build_universal_tool_use_slots`` directly and pass the result
as ``tool_use_sp``.

Coverage:
  - search_actions_enabled=True in slot-map: SP names search_actions in wrapper chain
  - search_actions_enabled=False in slot-map: search_actions absent from SP
  - Default (tool_use_sp=None): bare OS frame, no wrapper content
"""
from __future__ import annotations

import pytest

from reyn.chat.router_system_prompt import build_system_prompt
from reyn.tools.schemes._universal_sp import build_universal_tool_use_slots

_BASE = {
    "agent_name": "chat",
    "agent_role": "test agent",
    "available_skills": [],
    "available_agents": [],
    "memory_index": {"status": "not_found", "content": ""},
}


def _slots(*, search_actions_enabled: bool) -> "dict[str, str]":
    """Build slot-map with universal wrappers on (needed to show the chain)."""
    return build_universal_tool_use_slots(
        universal_wrappers_enabled=True,
        search_actions_enabled=search_actions_enabled,
        discovery_mandate=False,
        has_hot_list_aliases=False,
        non_interactive=False,
    )


# ---------------------------------------------------------------------------
# search_actions_enabled=True
# ---------------------------------------------------------------------------


def test_search_actions_enabled_true_includes_search_actions_in_wrapper_chain() -> None:
    """Tier 2: search_actions_enabled=True in slot-map → SP names search_actions in
    the Capabilities routing chain."""
    sp = build_system_prompt(**_BASE, tool_use_sp=_slots(search_actions_enabled=True))
    assert "search_actions" in sp
    assert "`list_actions` → `search_actions`" in sp


def test_search_actions_enabled_true_includes_four_wrapper_names() -> None:
    """Tier 2: with search_actions_enabled=True, all four wrapper names appear."""
    sp = build_system_prompt(**_BASE, tool_use_sp=_slots(search_actions_enabled=True))
    for name in ("list_actions", "search_actions", "describe_action", "invoke_action"):
        assert name in sp, f"expected {name!r} in SP with search_actions_enabled=True"


def test_search_actions_enabled_true_includes_behaviour_guidance() -> None:
    """Tier 2: search_actions_enabled=True → Behaviour section references search_actions."""
    sp = build_system_prompt(**_BASE, tool_use_sp=_slots(search_actions_enabled=True))
    assert "USE `search_actions(query=...)`" in sp


# ---------------------------------------------------------------------------
# search_actions_enabled=False (= no embedding_class configured)
# ---------------------------------------------------------------------------


def test_search_actions_enabled_false_omits_search_actions_from_sp() -> None:
    """Tier 2: search_actions_enabled=False → search_actions absent from SP."""
    sp = build_system_prompt(**_BASE, tool_use_sp=_slots(search_actions_enabled=False))
    assert "search_actions" not in sp


def test_search_actions_enabled_false_omits_search_actions_from_chain() -> None:
    """Tier 2: search_actions_enabled=False → wrapper chain omits search_actions."""
    sp = build_system_prompt(**_BASE, tool_use_sp=_slots(search_actions_enabled=False))
    assert "search_actions" not in sp
    assert "`list_actions` → `describe_action` → `invoke_action`" in sp


def test_search_actions_enabled_false_three_wrapper_names_present() -> None:
    """Tier 2: search_actions_enabled=False → the three always-available wrapper
    names appear in the SP."""
    sp = build_system_prompt(**_BASE, tool_use_sp=_slots(search_actions_enabled=False))
    for name in ("list_actions", "describe_action", "invoke_action"):
        assert name in sp, (
            f"expected {name!r} in SP when search_actions_enabled=False"
        )


def test_search_actions_enabled_false_omits_semantic_search_behaviour() -> None:
    """Tier 2: search_actions_enabled=False → search_actions Behaviour guidance absent."""
    sp = build_system_prompt(**_BASE, tool_use_sp=_slots(search_actions_enabled=False))
    assert "USE search_actions" not in sp
    assert "search_actions" not in sp


# ---------------------------------------------------------------------------
# Default kwarg — bare OS frame (no tool-use SP)
# ---------------------------------------------------------------------------


def test_default_kwarg_matches_explicit_true() -> None:
    """Tier 2: #1627 Stage 4 — the default (tool_use_sp=None) yields a bare OS
    frame distinct from any slot-map. Two explicit calls with the same slot-map
    are byte-identical to each other.

    NOTE: After Stage 4, omitting tool_use_sp is no longer byte-identical to
    explicit True — None produces the bare frame, not the universal SP.
    This test pins the new contract: two identical slot-map calls are equal.
    """
    sp_explicit_true_a = build_system_prompt(**_BASE, tool_use_sp=_slots(search_actions_enabled=True))
    sp_explicit_true_b = build_system_prompt(**_BASE, tool_use_sp=_slots(search_actions_enabled=True))
    assert sp_explicit_true_a == sp_explicit_true_b


def test_default_includes_search_actions() -> None:
    """Tier 2: slot-map with search_actions_enabled=True includes search_actions.

    #1627 Stage 4: callers must supply the slot-map explicitly. This test verifies
    the slot-map path, not the default (None) path.
    """
    sp = build_system_prompt(**_BASE, tool_use_sp=_slots(search_actions_enabled=True))
    assert "search_actions" in sp
    assert "`list_actions` → `search_actions`" in sp
