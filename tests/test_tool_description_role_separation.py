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

def test_invoke_action_description_contains_spawn_ack_priority_1() -> None:
    """Tier 2: invoke_action description carries spawn-ack Priority 1 (/tasks MUST).

    B23-PRE-1: spawn-ack Priority block moved from SP to invoke_action.description.
    """
    desc = _INVOKE_ACTION_DESCRIPTION
    assert "Priority 1" in desc
    assert "/tasks" in desc
    # Hard failure signal
    assert "hard failure" in desc or "non-negotiable" in desc


def test_invoke_action_description_contains_fabrication_by_construction() -> None:
    """Tier 2: invoke_action description carries anti-fabrication rule.

    B23-PRE-1: fabrication-by-construction rule moved from SP spawn-ack block.
    """
    desc = _INVOKE_ACTION_DESCRIPTION
    assert "fabrication by construction" in desc


def test_invoke_action_description_contains_optimism_bias() -> None:
    """Tier 2: invoke_action description carries Optimism bias / errors verbatim rule.

    B23-PRE-1: task_completed error handling moved from SP to invoke_action.description.
    """
    desc = _INVOKE_ACTION_DESCRIPTION
    # Either the phrase "Optimism bias" or the verbatim instruction
    assert "Optimism" in desc or "verbatim" in desc or "MUST surface" in desc


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
    """Tier 2: recall _HIDE_LEGACY description distinguishes recall from memory.entry.

    B23-PRE-1: recall vs memory disambiguation moved from SP disambiguation
    block to recall._RECALL_DESCRIPTION_HIDE_LEGACY.
    """
    desc = _RECALL_DESCRIPTION_HIDE_LEGACY
    assert "memory.entry" in desc or "memory" in desc
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
