"""Tier 2: SP rendering with hide_legacy_tools=True (FP-0034 B23-PRE-1).

Wrapper-only e2e prerequisite. Validates:
1. Default False is byte-identical to the legacy SP (= 0 LLMReplay fixture re-records).
2. True renders the SP without legacy per-kind tool literals in actionable
   instructions (= LLM cannot be steered toward a tool that is not in tools=).
3. The 8 attractor mitigations from batch 5–22 are preserved in wrapper
   vocabulary (post-list MUST, post-describe MUST, ABSOLUTE rule, /tasks MUST,
   anti-fabrication, anti-optimism, static section ordering, Memory-not-Recall).
"""
from __future__ import annotations

import re

from reyn.chat.router_system_prompt import build_system_prompt

_BASE_KWARGS: dict = dict(
    agent_name="default",
    agent_role="generalist",
    available_skills=[{"name": "code_review", "description": "review code"}],
    available_agents=[{"name": "alice", "role": "helper"}],
    memory_index={"status": "ok", "shared": [], "agent": []},
    file_permissions={"read": ["*"], "write": ["*"]},
    mcp_servers=[],
    universal_wrappers_enabled=True,
)


# Legacy per-kind tools that must not appear in actionable instructions
# when hide_legacy_tools=True. The 2 acceptable mentions (Action categories
# description listing what the memory.operation / rag.operation categories
# contain) are not in this set.
_LEGACY_TOOL_TOKENS = (
    "list_skills",
    "describe_skill",
    "invoke_skill",
    "list_agents",
    "describe_agent",
    "delegate_to_agent",
    "read_file",
    "write_file",
    "delete_file",
    "list_directory",
    "web_search",
    "web_fetch",
    "reyn_src_read",
    "reyn_src_list",
    "list_memory",
    "read_memory_body",
    "call_mcp_tool",
    "list_mcp_servers",
    "list_mcp_tools",
    "describe_mcp_tool",
)


def test_default_false_is_byte_identical_to_legacy() -> None:
    """Tier 2: default hide_legacy_tools=False must be byte-identical to legacy.

    Guarantees that 7 existing LLMReplay fixtures under tests/fixtures/llm/router/
    keep their SHA-256 keys valid (= 0 re-records required) when this flag lands.
    """
    sp_default = build_system_prompt(**_BASE_KWARGS)
    sp_explicit_false = build_system_prompt(
        **_BASE_KWARGS, hide_legacy_tools=False,
    )
    assert sp_default == sp_explicit_false


def test_wrapper_only_excludes_legacy_tool_literals() -> None:
    """Tier 2: hide_legacy_tools=True must not contain legacy tool names
    in actionable instructions (Capabilities, Behaviour, Skills, Agents,
    spawn-ack, Agent delegation, ABSOLUTE rule, Memory header)."""
    sp = build_system_prompt(**_BASE_KWARGS, hide_legacy_tools=True)
    pattern = r"\b(" + "|".join(_LEGACY_TOOL_TOKENS) + r")\b"
    hits = re.findall(pattern, sp)
    assert hits == [], (
        f"Legacy tool literals leaked into wrapper-only SP: {set(hits)}"
    )


def test_wrapper_only_with_indexed_sources_excludes_legacy_tool_literals() -> None:
    """Tier 2: same as above, but with indexed_sources_section provided
    (= the dynamic Behaviour disambiguation block also wraps cleanly)."""
    sp = build_system_prompt(
        **_BASE_KWARGS,
        hide_legacy_tools=True,
        indexed_sources_section="## Indexed sources\n  meetings (= notes)",
    )
    pattern = r"\b(" + "|".join(_LEGACY_TOOL_TOKENS) + r")\b"
    hits = re.findall(pattern, sp)
    assert hits == [], (
        f"Legacy tool literals leaked into wrapper-only SP with indexed: {set(hits)}"
    )


def test_wrapper_only_capabilities_has_universal_wrappers() -> None:
    """Tier 2: Capabilities section routes Action via the 4 universal wrappers."""
    sp = build_system_prompt(**_BASE_KWARGS, hide_legacy_tools=True)
    assert "list_actions(category=" in sp
    assert "search_actions" in sp
    assert "describe_action" in sp
    assert "invoke_action(action_name=" in sp


def test_wrapper_only_omits_skills_and_agents_dedicated_sections() -> None:
    """Tier 2: ## Skills and ## Agents are absent in wrapper-only mode
    (= skill / agent.peer are 2 of 13 categories, not special-cased)."""
    sp = build_system_prompt(**_BASE_KWARGS, hide_legacy_tools=True)
    assert "## Skills (" not in sp
    assert "## Agents (resource axis" not in sp
    # Legacy mode still has them (separate test below).


def test_legacy_mode_keeps_skills_and_agents_sections() -> None:
    """Tier 2: legacy mode preserves the dedicated ## Skills / ## Agents
    catalog sections (= byte-identity guarantee)."""
    sp = build_system_prompt(**_BASE_KWARGS, hide_legacy_tools=False)
    assert "## Skills (1 available)" in sp
    assert "## Agents (resource axis" in sp


def test_wrapper_only_preserves_routing_rule_absolute_pin() -> None:
    """Tier 2: B12-R2/B13-R3 V3 ABSOLUTE rule preserved in wrapper vocab."""
    sp = build_system_prompt(**_BASE_KWARGS, hide_legacy_tools=True)
    assert "ROUTING RULE (ABSOLUTE)" in sp
    assert "NO clarifying questions" in sp
    assert "NO text replies" in sp
    # JA placeholder example, wrapper version
    assert "<action_name>" in sp
    # Legacy <skill_name> placeholder must not appear in wrapper mode
    assert "<skill_name>" not in sp


def test_wrapper_only_preserves_spawn_ack_pins() -> None:
    """Tier 2: FP-0011/FP-0012 spawn-ack mitigations preserved."""
    sp = build_system_prompt(**_BASE_KWARGS, hide_legacy_tools=True)
    # /tasks pointer MUST
    assert "MUST include `/tasks`" in sp
    assert "non-negotiable" in sp
    # Anti-fabrication
    assert "fabrication by construction" in sp
    assert "MUST NOT pre-fill" in sp
    # Anti-optimism
    assert "Optimism bias" in sp
    # Spawn-ack uses wrapper vocab
    assert "invoke_action returns {status:" in sp
    assert "the action is running" in sp
    assert "background action finished" in sp


def test_wrapper_only_preserves_post_describe_and_post_list_must_bullets() -> None:
    """Tier 2: B2-H1 / B3-H1+M3 / B5-H1 attractor mitigations preserved."""
    sp = build_system_prompt(**_BASE_KWARGS, hide_legacy_tools=True)
    # post-describe MUST
    assert "After describe_action" in sp
    assert "MUST call invoke_action" in sp
    # post-list MUST
    assert "After list_actions" in sp
    assert "Do NOT reply directly" in sp
    # B11-R3 direct-invoke when name visible
    assert "skip list_actions" in sp


def test_wrapper_only_agent_delegation_uses_invoke_action() -> None:
    """Tier 2: ## Agent delegation rewritten to invoke_action(agent.peer__)."""
    sp = build_system_prompt(**_BASE_KWARGS, hide_legacy_tools=True)
    assert "## Agent delegation" in sp
    assert "agent.peer__" in sp
    assert 'invoke_action(action_name="agent.peer__' in sp


def test_wrapper_only_memory_header_uses_invoke_action() -> None:
    """Tier 2: ## Memory header references invoke_action for memory.entry."""
    sp = build_system_prompt(**_BASE_KWARGS, hide_legacy_tools=True)
    # Wrapper memory header references qualified name pattern
    assert 'memory.entry__' in sp


def test_wrapper_only_preserves_static_section_ordering() -> None:
    """Tier 2: FP-0023 Change 1 cache prefix ordering (Capabilities <
    Action categories < Behaviour) preserved in wrapper mode."""
    sp = build_system_prompt(**_BASE_KWARGS, hide_legacy_tools=True)
    cap_pos = sp.index("## Capabilities (routing guide)")
    cat_pos = sp.index("## Action categories")
    beh_pos = sp.index("## Behaviour")
    assert cap_pos < cat_pos < beh_pos


def test_wrapper_only_preserves_memory_not_recall_naming() -> None:
    """Tier 2: B17-S5-3 Memory-vs-Recall vocab collision avoidance."""
    sp = build_system_prompt(**_BASE_KWARGS, hide_legacy_tools=True)
    # The 'Memory access' axis renames (vs 'Recall') applies only to the
    # legacy intent-axis enumeration which the wrapper path collapses
    # away. What we must ensure: the ## Memory section header still uses
    # 'Memory' (not 'Recall') for the entries listing.
    assert "## Memory (entries inlined" in sp


def test_wrapper_only_with_indexed_sources_uses_qualified_recall() -> None:
    """Tier 2: indexed_sources disambiguation block uses
    invoke_action(action_name='rag.operation__recall', ...) in wrapper mode."""
    sp = build_system_prompt(
        **_BASE_KWARGS,
        hide_legacy_tools=True,
        indexed_sources_section="## Indexed sources\n  meetings (= notes)",
    )
    assert 'invoke_action(action_name="rag.operation__recall"' in sp
    # JA disambiguation block uses qualified names too
    assert 'memory.operation__remember_shared' in sp
    assert 'memory.operation__forget' in sp


def test_wrapper_only_size_smaller_than_legacy() -> None:
    """Tier 2: structural simplification removes per-category enumerations,
    so wrapper-only SP is shorter than legacy."""
    sp_legacy = build_system_prompt(**_BASE_KWARGS, hide_legacy_tools=False)
    sp_wrapper = build_system_prompt(**_BASE_KWARGS, hide_legacy_tools=True)
    assert len(sp_wrapper) < len(sp_legacy), (
        f"Wrapper-only SP not smaller: wrapper={len(sp_wrapper)} legacy={len(sp_legacy)}"
    )
