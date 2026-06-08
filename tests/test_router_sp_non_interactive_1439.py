"""Tier 2: #1439 Fix #1 — autonomy-mode router SP signal (run-once path).

`build_system_prompt` renders "ask ONE clarifying question instead of guessing"
for the interactive router. In run-once (piped stdin, no TTY) there is no user
to answer, so that directive is a structural dead-end (the agent stalls asking,
13398). The `non_interactive` flag swaps that one directive for a proceed-with-
assumption directive; everything else (and the whole interactive SP) is unchanged.

Real `build_system_prompt`, no mocks.
"""
from __future__ import annotations

from reyn.chat.router_system_prompt import build_system_prompt

_CLARIFY = "ask ONE"
_PROCEED = "no interactive user to ask"


def _sp(*, non_interactive: bool) -> str:
    return build_system_prompt(
        agent_name="chat",
        agent_role="general agent",
        available_skills=[],
        available_agents=[],
        memory_index={},
        non_interactive=non_interactive,
    )


def test_interactive_default_keeps_clarifying_question() -> None:
    """Tier 2: #1439 Fix #1 — the default (interactive) SP keeps the "ask ONE
    clarifying question" directive = byte-identical to pre-#1439 behaviour."""
    sp = _sp(non_interactive=False)
    assert _CLARIFY in sp
    assert _PROCEED not in sp


def test_non_interactive_replaces_clarifying_with_proceed() -> None:
    """Tier 2: #1439 Fix #1 — non_interactive (run-once) omits the clarifying-
    question directive and instead tells the agent to proceed with an assumption
    (no user to ask). This is the 13398 dead-stop fix."""
    sp = _sp(non_interactive=True)
    assert _CLARIFY not in sp, "run-once SP must not tell the agent to ask a clarifying question"
    assert _PROCEED in sp
    # show-not-judge of the directive: it tells it to proceed, not to stop.
    assert "proceed" in sp.lower()


def test_default_param_is_interactive() -> None:
    """Tier 2: #1439 Fix #1 — omitting the param defaults to interactive
    (byte-identical), so every existing caller is unaffected."""
    default_sp = build_system_prompt(
        agent_name="chat",
        agent_role="general agent",
        available_skills=[],
        available_agents=[],
        memory_index={},
    )
    assert default_sp == _sp(non_interactive=False)
