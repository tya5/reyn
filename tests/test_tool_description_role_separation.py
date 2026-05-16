"""Tier 2: Tool description role separation (FP-0034 B23-PRE-1).

Validates that content removed from the SP wrapper-only path has landed
in the respective tool descriptions (= Anthropic 1-tool-1-purpose pattern).

Tested tool descriptions:
- _INVOKE_ACTION_DESCRIPTION_HIDE_LEGACY: spawn-ack, task_completed, agent delegation
- _LIST_ACTIONS_DESCRIPTION_HIDE_LEGACY: POST_CALL MUST chain
- _DESCRIBE_ACTION_DESCRIPTION_HIDE_LEGACY: POST_CALL MUST chain
- _SEARCH_ACTIONS_DESCRIPTION_HIDE_LEGACY: multilingual + POST_CALL
- _PLAN_DESCRIPTION_HIDE_LEGACY: WHAT/WHEN/WHEN_NOT (absorbed SP subsection)
- _RECALL_DESCRIPTION_HIDE_LEGACY: recall vs memory disambiguation, multilingual
- _REMEMBER_SHARED_DESCRIPTION_HIDE_LEGACY: language-agnostic intent triggers
- _FORGET_MEMORY_DESCRIPTION_HIDE_LEGACY: language-agnostic intent triggers
"""
from __future__ import annotations

from reyn.tools.memory import (
    _FORGET_MEMORY_DESCRIPTION_HIDE_LEGACY,
    _REMEMBER_SHARED_DESCRIPTION_HIDE_LEGACY,
)
from reyn.tools.plan import _PLAN_DESCRIPTION_HIDE_LEGACY
from reyn.tools.recall import _RECALL_DESCRIPTION_HIDE_LEGACY
from reyn.tools.universal_catalog import (
    _DESCRIBE_ACTION_DESCRIPTION_HIDE_LEGACY,
    _INVOKE_ACTION_DESCRIPTION_HIDE_LEGACY,
    _LIST_ACTIONS_DESCRIPTION_HIDE_LEGACY,
    _SEARCH_ACTIONS_DESCRIPTION_HIDE_LEGACY,
)

# ── invoke_action description tests ────────────────────────────────────────────

def test_invoke_action_description_contains_spawn_ack_priority_1() -> None:
    """Tier 2: invoke_action description carries spawn-ack Priority 1 (/tasks MUST).

    B23-PRE-1: spawn-ack Priority block moved from SP to invoke_action.description.
    """
    desc = _INVOKE_ACTION_DESCRIPTION_HIDE_LEGACY
    assert "Priority 1" in desc
    assert "/tasks" in desc
    # Hard failure signal
    assert "hard failure" in desc or "non-negotiable" in desc


def test_invoke_action_description_contains_fabrication_by_construction() -> None:
    """Tier 2: invoke_action description carries anti-fabrication rule.

    B23-PRE-1: fabrication-by-construction rule moved from SP spawn-ack block.
    """
    desc = _INVOKE_ACTION_DESCRIPTION_HIDE_LEGACY
    assert "fabrication by construction" in desc


def test_invoke_action_description_contains_optimism_bias() -> None:
    """Tier 2: invoke_action description carries Optimism bias / errors verbatim rule.

    B23-PRE-1: task_completed error handling moved from SP to invoke_action.description.
    """
    desc = _INVOKE_ACTION_DESCRIPTION_HIDE_LEGACY
    # Either the phrase "Optimism bias" or the verbatim instruction
    assert "Optimism" in desc or "verbatim" in desc or "MUST surface" in desc


def test_invoke_action_description_contains_agent_delegation_pattern() -> None:
    """Tier 2: invoke_action description carries agent.peer delegation pattern.

    B23-PRE-1: ## Agent delegation SP subsection moved to invoke_action.description.
    """
    desc = _INVOKE_ACTION_DESCRIPTION_HIDE_LEGACY
    assert "agent.peer__" in desc
    # The description key or pattern
    assert "AGENT DELEGATION" in desc or "delegation" in desc.lower()


def test_invoke_action_description_contains_task_completed_handling() -> None:
    """Tier 2: invoke_action description carries task_completed narration guidance.

    B23-PRE-1: task_completed handling moved from SP to invoke_action.description.
    """
    desc = _INVOKE_ACTION_DESCRIPTION_HIDE_LEGACY
    assert "[task_completed]" in desc or "task_completed" in desc
    assert "TASK_COMPLETED" in desc or "task_completed" in desc


# ── list_actions description tests ─────────────────────────────────────────────

def test_list_actions_description_contains_post_call_must() -> None:
    """Tier 2: list_actions description carries POST_CALL MUST chain.

    B23-PRE-1: post-list MUST chain moved from SP Behaviour bullets to
    list_actions.description per 1-tool-1-purpose pattern.
    """
    desc = _LIST_ACTIONS_DESCRIPTION_HIDE_LEGACY
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
    desc = _DESCRIBE_ACTION_DESCRIPTION_HIDE_LEGACY
    assert "POST_CALL" in desc
    assert "MUST" in desc
    assert "invoke_action" in desc


# ── search_actions description tests ───────────────────────────────────────────

def test_search_actions_description_is_multilingual() -> None:
    """Tier 2: search_actions description emphasizes multilingual support.

    B23-PRE-1: multilingual emphasis added to search_actions description.
    """
    desc = _SEARCH_ACTIONS_DESCRIPTION_HIDE_LEGACY
    assert "multilingual" in desc.lower() or "any language" in desc.lower()


def test_search_actions_description_contains_post_call_must() -> None:
    """Tier 2: search_actions description carries POST_CALL MUST chain."""
    desc = _SEARCH_ACTIONS_DESCRIPTION_HIDE_LEGACY
    assert "POST_CALL" in desc
    assert "MUST" in desc


# ── plan description tests ──────────────────────────────────────────────────────

def test_plan_description_contains_when_not_single_tool_lookups() -> None:
    """Tier 2: plan _HIDE_LEGACY description carries WHEN NOT clause.

    B23-PRE-1: ## Plan decomposition SP subsection absorbed into plan.description.
    """
    desc = _PLAN_DESCRIPTION_HIDE_LEGACY
    assert "WHEN NOT" in desc
    assert "single-tool" in desc.lower() or "Single-tool" in desc


def test_plan_description_contains_multi_source_examples() -> None:
    """Tier 2: plan _HIDE_LEGACY description contains multi-source examples."""
    desc = _PLAN_DESCRIPTION_HIDE_LEGACY
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

def test_remember_shared_description_is_multilingual() -> None:
    """Tier 2: remember_shared _HIDE_LEGACY description includes multilingual intent triggers.

    B23-PRE-1: memory write triggers (覚えて / remember etc.) moved from
    SP Plan decomposition subsection to _REMEMBER_SHARED_DESCRIPTION_HIDE_LEGACY.
    """
    desc = _REMEMBER_SHARED_DESCRIPTION_HIDE_LEGACY
    # Must include at least one non-EN trigger word
    assert "覚えて" in desc or "メモして" in desc or "multilingual" in desc.lower()
    # Must include EN triggers
    assert "remember" in desc.lower() or "save" in desc.lower()


# ── forget_memory description tests ────────────────────────────────────────────

def test_forget_memory_description_is_multilingual() -> None:
    """Tier 2: forget_memory _HIDE_LEGACY description includes multilingual intent triggers.

    B23-PRE-1: forget triggers (忘れて / delete etc.) moved from SP
    JA disambiguation table to _FORGET_MEMORY_DESCRIPTION_HIDE_LEGACY.
    """
    desc = _FORGET_MEMORY_DESCRIPTION_HIDE_LEGACY
    # Must include at least one non-EN trigger word
    assert "忘れて" in desc or "削除して" in desc or "multilingual" in desc.lower()
    # Must include EN triggers
    assert "forget" in desc.lower() or "delete" in desc.lower()
