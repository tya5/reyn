"""Tier 2: Tool description role separation (FP-0034 B23-PRE-1).

Validates that content removed from the SP wrapper-only path has landed
in the respective tool descriptions (= Anthropic 1-tool-1-purpose pattern).

Tested tool descriptions:
- _INVOKE_ACTION_DESCRIPTION: spawn-ack, task_completed, agent delegation
- _LIST_ACTIONS_DESCRIPTION: POST_CALL MUST chain
- _DESCRIBE_ACTION_DESCRIPTION: POST_CALL MUST chain
- _SEARCH_ACTIONS_DESCRIPTION: multilingual + POST_CALL
- _PLAN_DESCRIPTION: multi-source examples
- _RECALL_DESCRIPTION_HIDE_LEGACY: recall vs memory disambiguation, multilingual
"""
from __future__ import annotations

from reyn.tools.memory import (
    _FORGET_MEMORY_DESCRIPTION,
    _REMEMBER_SHARED_DESCRIPTION,
)
from reyn.tools.plan import _PLAN_DESCRIPTION
from reyn.tools.recall import _RECALL_DESCRIPTION_HIDE_LEGACY
from reyn.tools.universal_catalog import (
    _DESCRIBE_ACTION_DESCRIPTION,
    _INVOKE_ACTION_DESCRIPTION,
    _LIST_ACTIONS_DESCRIPTION,
    _SEARCH_ACTIONS_DESCRIPTION,
)

# ── invoke_action description tests ────────────────────────────────────────────

def test_invoke_action_description_signals_os_owned_spawn_ack() -> None:
    """Tier 2: invoke_action description tells the LLM that the OS — not it —
    composes the spawn-ack user-visible reply.

    2026-05-17 N3 update: the spawn-ack Priority 1-4 block previously lived
    in this description (B23-PRE-1 role-separation) but was structurally
    unreachable code (router_loop exits the loop before the LLM is asked
    to compose anything for the spawn-ack turn — see ``_SPAWN_ACK_MSG`` in
    ``router_loop.py`` for the deterministic OS message that replaced it).

    The description must now signal that the LLM will NOT be asked to
    compose the spawn-ack reply, so it doesn't internalise a dead policy.
    """
    desc = _INVOKE_ACTION_DESCRIPTION
    assert "SPAWN-ACK" in desc, (
        "description must still mention the spawn-ack shape so the LLM "
        "recognises {status: 'spawned'} in non-router contexts; got: "
        f"{desc[:200]!r}"
    )
    assert "OS emits" in desc or "router exits" in desc, (
        "description must clarify that the OS owns the spawn-ack reply, "
        "not the LLM. Either 'OS emits' or 'router exits' phrasing is "
        f"acceptable; got: {desc[:300]!r}"
    )


def test_invoke_action_description_no_dead_priority_block() -> None:
    """Tier 2: regression guard: the description must NOT carry the obsolete
    'Priority 1/2/3/4' spawn-ack block. That block was dead code (the router
    exits before the LLM sees it) and reintroducing it would re-confuse the
    LLM about whether it's responsible for the spawn-ack reply.
    """
    desc = _INVOKE_ACTION_DESCRIPTION
    assert "Priority 1" not in desc, (
        "description must not reintroduce the dead 'Priority 1' spawn-ack "
        f"block; got: {desc[:400]!r}"
    )
    assert "fabrication by construction" not in desc, (
        "description must not reintroduce the dead 'fabrication by "
        f"construction' clause from the obsolete spawn-ack block; got: "
        f"{desc[:400]!r}"
    )


def test_invoke_action_description_carries_task_completed_status_semantics() -> None:
    """Tier 2: invoke_action description carries the TASK_COMPLETED meaning,
    including how the LLM should read non-'finished' status values.

    B23-PRE-1: task_completed handling moved from SP to
    invoke_action.description.
    B49 W1-S6 fix (2026-05-22): the prescriptive "MUST surface verbatim"
    + "narrate in 1-2 sentences" + "Optimism bias" handling rules were
    removed per the SP-conveys-meaning / LLM-decides-handling principle.
    The description now lists the non-'finished' status values (=
    ``loop_limit_exceeded``, ``phase_budget_exceeded``,
    ``budget_exceeded``, ``error``) so the LLM knows their semantic and
    can judge how to narrate.
    """
    desc = _INVOKE_ACTION_DESCRIPTION
    assert "TASK_COMPLETED" in desc, (
        f"description should anchor TASK_COMPLETED handling; got "
        f"{desc[:400]!r}"
    )
    # The non-'finished' status values must be enumerated so the LLM
    # knows what a failure status looks like.
    for status_value in ("loop_limit_exceeded", "budget_exceeded", "error"):
        assert status_value in desc, (
            f"TASK_COMPLETED block should name status='{status_value}' so "
            f"the LLM recognises non-success states; got {desc[:400]!r}"
        )


def test_invoke_action_description_contains_agent_delegation_pattern() -> None:
    """Tier 2: invoke_action description carries agent.peer delegation pattern.

    B23-PRE-1: ## Agent delegation SP subsection moved to invoke_action.description.
    """
    desc = _INVOKE_ACTION_DESCRIPTION
    assert "agent.peer__" in desc
    # The description key or pattern
    assert "AGENT DELEGATION" in desc or "delegation" in desc.lower()


def test_invoke_action_description_contains_task_completed_handling() -> None:
    """Tier 2: invoke_action description carries task_completed narration guidance.

    B23-PRE-1: task_completed handling moved from SP to invoke_action.description.
    """
    desc = _INVOKE_ACTION_DESCRIPTION
    assert "[task_completed]" in desc or "task_completed" in desc
    assert "TASK_COMPLETED" in desc or "task_completed" in desc


# ── list_actions description tests ─────────────────────────────────────────────

def test_list_actions_description_contains_post_call_must() -> None:
    """Tier 2: list_actions description carries POST_CALL MUST chain.

    B23-PRE-1: post-list MUST chain moved from SP Behaviour bullets to
    list_actions.description per 1-tool-1-purpose pattern.
    """
    desc = _LIST_ACTIONS_DESCRIPTION
    assert "POST_CALL" in desc
    assert "MUST" in desc
    # Must reference the next tool (describe_action or invoke_action)
    assert "describe_action" in desc or "invoke_action" in desc


# ── describe_action description tests ──────────────────────────────────────────

def test_describe_action_description_contains_post_call_must() -> None:
    """Tier 2: describe_action description carries POST_CALL MUST chain.

    B23-PRE-1: post-describe MUST chain moved from SP Behaviour bullets to
    describe_action.description per 1-tool-1-purpose pattern.
    """
    desc = _DESCRIBE_ACTION_DESCRIPTION
    assert "POST_CALL" in desc
    assert "MUST" in desc
    assert "invoke_action" in desc


# ── search_actions description tests ───────────────────────────────────────────

def test_search_actions_description_is_multilingual() -> None:
    """Tier 2: search_actions description emphasizes multilingual support.

    B23-PRE-1: multilingual emphasis added to search_actions description.
    """
    desc = _SEARCH_ACTIONS_DESCRIPTION
    assert "multilingual" in desc.lower() or "any language" in desc.lower()


def test_search_actions_description_contains_post_call_must() -> None:
    """Tier 2: search_actions description carries POST_CALL MUST chain."""
    desc = _SEARCH_ACTIONS_DESCRIPTION
    assert "POST_CALL" in desc
    assert "MUST" in desc


# ── plan description tests ──────────────────────────────────────────────────────

def test_plan_description_contains_multi_source_examples() -> None:
    """Tier 2: plan description contains multi-source examples."""
    desc = _PLAN_DESCRIPTION
    assert "multi" in desc.lower()
    # Should reference the compare/explain/summarise pattern
    assert "compare" in desc.lower() or "multiple independent" in desc.lower()


# ── recall description tests ───────────────────────────────────────────────────

def test_recall_description_contains_disambiguation_with_memory() -> None:
    """Tier 2: recall _HIDE_LEGACY description distinguishes recall from memory_entry.

    B23-PRE-1: recall vs memory disambiguation moved from SP disambiguation
    block to recall._RECALL_DESCRIPTION_HIDE_LEGACY.
    """
    desc = _RECALL_DESCRIPTION_HIDE_LEGACY
    assert "memory_entry" in desc or "memory" in desc
    # The description must explicitly note the anti-confusion rule
    assert "recall" in desc.lower()
    # The disambiguation signal
    assert "NOT" in desc or "WHEN NOT" in desc


def test_recall_description_is_multilingual() -> None:
    """Tier 2: recall _HIDE_LEGACY description mentions multilingual capability."""
    desc = _RECALL_DESCRIPTION_HIDE_LEGACY
    assert "multilingual" in desc.lower() or "any language" in desc.lower()


# ── remember_shared description tests ──────────────────────────────────────────

def test_remember_shared_description_present() -> None:
    """Tier 2: remember_shared description is present and non-empty.

    B23-PRE-1: _REMEMBER_SHARED_DESCRIPTION is the canonical description
    (enriched _HIDE_LEGACY variant was merged into the canonical name).
    """
    desc = _REMEMBER_SHARED_DESCRIPTION
    assert len(desc) > 0
    assert "memory" in desc.lower() or "persist" in desc.lower()


# ── forget_memory description tests ────────────────────────────────────────────

def test_forget_memory_description_present() -> None:
    """Tier 2: forget_memory description is present and non-empty.

    B23-PRE-1: _FORGET_MEMORY_DESCRIPTION is the canonical description
    (enriched _HIDE_LEGACY variant was merged into the canonical name).
    """
    desc = _FORGET_MEMORY_DESCRIPTION
    assert len(desc) > 0
    assert "forget" in desc.lower() or "delete" in desc.lower()
