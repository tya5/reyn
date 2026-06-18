"""Tier 2: #1439 Fix #1 — autonomy-mode router SP signal (run-once path).

#1627 Stage 4: ``build_system_prompt`` is now a pure slot-injector. The
``non_interactive`` parameter has been REMOVED from ``build_system_prompt``.
Tests now call ``build_universal_tool_use_slots`` directly (with
``non_interactive=True/False``) and pass the result as ``tool_use_sp``.

In run-once (piped stdin, no TTY) there is no user to answer, so the
"ask ONE clarifying question" directive is a structural dead-end (stalls
the agent, #13398). The ``non_interactive`` flag in
``build_universal_tool_use_slots`` swaps that one directive for a
proceed-with-assumption directive; everything else is unchanged.

Real ``build_system_prompt`` + real ``build_universal_tool_use_slots``,
no mocks.
"""
from __future__ import annotations

from reyn.runtime.router_system_prompt import build_system_prompt
from reyn.tools.schemes._universal_sp import build_universal_tool_use_slots

_CLARIFY = "ask ONE"
_PROCEED = "no interactive user to ask"


def _sp(*, non_interactive: bool) -> str:
    slots = build_universal_tool_use_slots(
        universal_wrappers_enabled=False,
        search_actions_enabled=True,
        discovery_mandate=False,
        has_hot_list_aliases=False,
        non_interactive=non_interactive,
    )
    return build_system_prompt(
        agent_name="chat",
        agent_role="general agent",
        available_skills=[],
        available_agents=[],
        memory_index={},
        tool_use_sp=slots,
    )


def test_interactive_default_keeps_clarifying_question() -> None:
    """Tier 2: #1439 Fix #1 — non_interactive=False keeps the "ask ONE
    clarifying question" directive."""
    sp = _sp(non_interactive=False)
    assert _CLARIFY in sp
    assert _PROCEED not in sp


def test_non_interactive_replaces_clarifying_with_proceed() -> None:
    """Tier 2: #1439 Fix #1 — non_interactive=True omits the clarifying-
    question directive and instead tells the agent to proceed with an assumption
    (no user to ask). This is the 13398 dead-stop fix."""
    sp = _sp(non_interactive=True)
    assert _CLARIFY not in sp, "run-once SP must not tell the agent to ask a clarifying question"
    assert _PROCEED in sp
    assert "proceed" in sp.lower()


def test_default_param_is_interactive() -> None:
    """Tier 2: #1439 Fix #1 — omitting non_interactive defaults to interactive
    (byte-identical to False), so every existing caller is unaffected."""
    default_sp = build_system_prompt(
        agent_name="chat",
        agent_role="general agent",
        available_skills=[],
        available_agents=[],
        memory_index={},
        tool_use_sp=build_universal_tool_use_slots(
            universal_wrappers_enabled=False,
            search_actions_enabled=True,
            discovery_mandate=False,
            has_hot_list_aliases=False,
            non_interactive=False,
        ),
    )
    assert default_sp == _sp(non_interactive=False)
