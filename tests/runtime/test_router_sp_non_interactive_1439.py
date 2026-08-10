"""Tier 2: #1439 Fix #1 — autonomy-mode router SP signal (run-once path).

sp-autonomy-revision (2026-07): the ambiguity/proceed-vs-ask directive was
promoted from the scheme-owned ``_universal_sp.py`` fork to the OS-frame
``build_system_prompt(non_interactive=...)`` Behaviour rule, so it reaches
every tool-use scheme (universal / enumerate / retrieval / CodeAct), not just
the universal-category path. ``build_universal_tool_use_slots`` still accepts
``non_interactive`` for backward compat, but no longer branches on it — the
fork now lives solely in ``build_system_prompt``.

In run-once (piped stdin, no TTY) there is no user to answer, so an ask-first
directive is a structural dead-end (stalls the agent, #13398).
``build_system_prompt(non_interactive=True)`` swaps in a proceed-with-
assumption directive; everything else is unchanged.

Real ``build_system_prompt``, no mocks.
"""
from __future__ import annotations

from reyn.runtime.router_system_prompt import build_system_prompt

# Substrings unique to each branch's wording (behavior-pinned, not a full-text
# snapshot): the non_interactive branch leads with an unconditional "default
# to proceeding" framing; the interactive branch leads with a softer
# "prefer proceeding" framing that still allows asking.
_NON_INTERACTIVE_ONLY = "make the most reasonable assumption, state it explicitly"
_INTERACTIVE_ONLY = "prefer proceeding with a stated,"
_ASK_ONE_COMMON = "Ask ONE targeted clarifying question ONLY when the ambiguity is BOTH"


def _sp(*, non_interactive: bool) -> str:
    return build_system_prompt(
        agent_name="chat",
        agent_role="general agent",
        available_agents=[],
        memory_index={},
        non_interactive=non_interactive,
    )


def test_interactive_default_keeps_prefer_proceeding_wording() -> None:
    """Tier 2: #1439 Fix #1 — non_interactive=False keeps the interactive
    "prefer proceeding" wording, and the shared one-question cap."""
    sp = _sp(non_interactive=False)
    assert _INTERACTIVE_ONLY in sp
    assert _NON_INTERACTIVE_ONLY not in sp
    assert _ASK_ONE_COMMON in sp


def test_non_interactive_uses_unconditional_proceed_wording() -> None:
    """Tier 2: #1439 Fix #1 — non_interactive=True swaps in the unconditional
    "default to proceeding" wording (no user to ask). This is the 13398
    dead-stop fix, now living in the OS frame."""
    sp = _sp(non_interactive=True)
    assert _NON_INTERACTIVE_ONLY in sp
    assert _INTERACTIVE_ONLY not in sp
    assert _ASK_ONE_COMMON in sp
    assert "proceed" in sp.lower()


def test_default_param_is_interactive() -> None:
    """Tier 2: #1439 Fix #1 — omitting non_interactive defaults to interactive
    (byte-identical to False), so every existing caller is unaffected."""
    default_sp = build_system_prompt(
        agent_name="chat",
        agent_role="general agent",
        available_agents=[],
        memory_index={},
    )
    assert default_sp == _sp(non_interactive=False)
