"""Tier 2: FP-0034 §D14 — search_actions SP wrapper visibility gate.

Verifies that ``build_system_prompt`` renders the wrapper enumeration line
and search_actions Behaviour guidance conditionally based on
``search_actions_enabled``.

Defect context (N5 probe, 2026-05-17):
  The weak default model ``gemini-2.5-flash-lite`` read the hardcoded SP
  line "4 universal wrappers: list_actions / search_actions / describe_action
  / invoke_action" and hallucinated a call to ``search_actions(query="...")``.
  Since ``embedding_class`` was not configured, ``search_actions`` was
  excluded from ``tools=`` — the dispatcher returned an ``unknown_tool``
  error. The model did NOT recover (did not fall back to list_actions or a
  hot-list alias); it gave up. This PR gates the enumeration on
  ``_search_visible`` (= D14 embedding-class gate) so the SP and ``tools=``
  stay in sync.

Coverage:
  - search_actions_enabled=True (default): SP says "4 universal wrappers"
    and includes search_actions in both the wrapper line and Behaviour
    guidance.
  - search_actions_enabled=False: SP says "3 universal wrappers" and
    search_actions is absent from the SP entirely.
  - Default (no kwarg): byte-identical to explicit True (backward compat
    for existing LLMReplay fixtures).
  - When universal_wrappers_enabled=False: search_actions_enabled is
    irrelevant to the wrapper line (the line is always emitted for compat).

No mocks — pure-string contract tests on build_system_prompt output.
"""
from __future__ import annotations

import pytest

from reyn.chat.router_system_prompt import build_system_prompt

_BASE = {
    "agent_name": "chat",
    "agent_role": "test agent",
    "available_skills": [],
    "available_agents": [],
    "memory_index": {"status": "not_found", "content": ""},
}


# ---------------------------------------------------------------------------
# search_actions_enabled=True (default)
# ---------------------------------------------------------------------------


def test_search_actions_enabled_true_includes_search_actions_in_wrapper_line() -> None:
    """Tier 2: search_actions_enabled=True → SP wrapper line says '4 universal wrappers'
    and names search_actions.

    This is the path when action_retrieval.embedding_class is configured and
    the ActionEmbeddingIndex is_ready(). The SP matches the tools= shape.
    """
    sp = build_system_prompt(**_BASE, search_actions_enabled=True)
    assert "search_actions" in sp
    assert "4 universal wrappers:" in sp


def test_search_actions_enabled_true_includes_four_wrapper_names() -> None:
    """Tier 2: with search_actions_enabled=True, all four wrapper names appear
    in the wrapper enumeration line.
    """
    sp = build_system_prompt(**_BASE, search_actions_enabled=True)
    for name in ("list_actions", "search_actions", "describe_action", "invoke_action"):
        assert name in sp, f"expected {name!r} in SP with search_actions_enabled=True"


def test_search_actions_enabled_true_includes_behaviour_guidance() -> None:
    """Tier 2: search_actions_enabled=True → Behaviour section references search_actions.

    The routing guidance "USE search_actions(query=...)" for semantic queries
    must only appear when search_actions is actually in tools=.
    """
    sp = build_system_prompt(**_BASE, search_actions_enabled=True)
    assert "USE search_actions(query=...)" in sp


# ---------------------------------------------------------------------------
# search_actions_enabled=False (= no embedding_class configured)
# ---------------------------------------------------------------------------


def test_search_actions_enabled_false_omits_search_actions_from_sp() -> None:
    """Tier 2: search_actions_enabled=False → search_actions absent from SP.

    This is the path when no embedding_class is configured (= default env).
    The LLM must not see search_actions in the SP because it is not in
    tools=, which would invite hallucinated calls (N5 finding).
    """
    sp = build_system_prompt(**_BASE, search_actions_enabled=False)
    assert "search_actions" not in sp


def test_search_actions_enabled_false_says_three_wrappers() -> None:
    """Tier 2: search_actions_enabled=False → SP says '3 universal wrappers'.

    The count must match the actual tools= shape so the LLM has no incentive
    to expect a fourth wrapper.
    """
    sp = build_system_prompt(**_BASE, search_actions_enabled=False)
    assert "3 universal wrappers:" in sp
    assert "4 universal wrappers:" not in sp


def test_search_actions_enabled_false_three_wrapper_names_present() -> None:
    """Tier 2: search_actions_enabled=False → the three always-available wrapper
    names appear in the SP.
    """
    sp = build_system_prompt(**_BASE, search_actions_enabled=False)
    for name in ("list_actions", "describe_action", "invoke_action"):
        assert name in sp, (
            f"expected {name!r} in SP when search_actions_enabled=False"
        )


def test_search_actions_enabled_false_omits_semantic_search_behaviour() -> None:
    """Tier 2: search_actions_enabled=False → search_actions Behaviour guidance absent.

    The 'USE search_actions(query=...)' routing hint must only appear when
    the tool is available, otherwise the LLM gets conflicting signals.
    """
    sp = build_system_prompt(**_BASE, search_actions_enabled=False)
    assert "USE search_actions" not in sp
    assert "search_actions" not in sp


# ---------------------------------------------------------------------------
# Default kwarg — byte-compat
# ---------------------------------------------------------------------------


def test_default_kwarg_matches_explicit_true() -> None:
    """Tier 2: omitting search_actions_enabled is byte-identical to explicit True.

    This preserves byte-compat for existing LLMReplay fixtures recorded
    before the search_actions_enabled flag was introduced. Those fixtures
    include search_actions in the SP and must continue to match.
    """
    sp_default = build_system_prompt(**_BASE)
    sp_explicit_true = build_system_prompt(**_BASE, search_actions_enabled=True)
    assert sp_default == sp_explicit_true, (
        "default search_actions_enabled must produce byte-identical SP to "
        "explicit True — existing fixture keys depend on this"
    )


def test_default_includes_search_actions() -> None:
    """Tier 2: default SP includes search_actions (backward compat).

    Existing fixtures were recorded with 'search_actions' in the SP. The
    default (True) must preserve this so fixture replay keys stay valid.
    """
    sp = build_system_prompt(**_BASE)
    assert "search_actions" in sp
    assert "4 universal wrappers:" in sp
