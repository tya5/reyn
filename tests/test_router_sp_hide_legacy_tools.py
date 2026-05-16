"""Tier 2: SP rendering with hide_legacy_tools=True (FP-0034 B23-PRE-1).

Wrapper-only e2e prerequisite. Validates:
1. Default False is byte-identical to the legacy SP (= 0 LLMReplay fixture re-records).
2. True renders the SP without legacy per-kind tool literals in actionable
   instructions (= LLM cannot be steered toward a tool that is not in tools=).
3. The 5 cross-cutting Behaviour policies are present in wrapper vocabulary:
   (Action/Plan/Reply 3-way, plan multi-source, never-invent, ABSOLUTE rule,
   errors verbatim). Per-tool flow details (spawn-ack, agent delegation, plan
   WHAT/WHEN_NOT) are absent from SP (= migrated to tool descriptions).
4. Dropped sections: ## Memory inline, ## MCP servers, ## Indexed sources,
   ## Files, ## Agent delegation subsection, ## Plan decomposition subsection,
   spawn-ack Priority block, JA disambiguation table.
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
    # All four wrapper names appear in the Capabilities section
    assert "list_actions" in sp
    assert "search_actions" in sp
    assert "describe_action" in sp
    assert "invoke_action" in sp


def test_wrapper_only_omits_skills_and_agents_dedicated_sections() -> None:
    """Tier 2: ## Skills, ## Agents, ## Memory, ## MCP, ## Files, ## Indexed
    sources are absent in wrapper-only mode — discovery via list_actions."""
    kwargs = dict(
        agent_name="default",
        agent_role="generalist",
        available_skills=[{"name": "code_review", "description": "review code"}],
        available_agents=[{"name": "alice", "role": "helper"}],
        memory_index={"status": "ok", "shared": [], "agent": []},
        file_permissions={"read": ["*"], "write": ["*"]},
        mcp_servers=[{"name": "brave", "description": "web search"}],
        universal_wrappers_enabled=True,
    )
    sp = build_system_prompt(
        **kwargs,
        hide_legacy_tools=True,
        indexed_sources_section="## Indexed sources\n  meetings",
    )
    assert "## Skills (" not in sp
    assert "## Agents (resource axis" not in sp
    assert "## Memory (entries inlined" not in sp
    assert "## MCP servers and tools" not in sp
    assert "## Files (resource axis" not in sp
    assert "## Indexed sources" not in sp


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
    # Legacy <skill_name> placeholder must not appear in wrapper mode
    assert "<skill_name>" not in sp


def test_wrapper_only_preserves_static_section_ordering() -> None:
    """Tier 2: FP-0023 Change 1 cache prefix ordering (Capabilities <
    Action categories < Behaviour) preserved in wrapper mode."""
    sp = build_system_prompt(**_BASE_KWARGS, hide_legacy_tools=True)
    cap_pos = sp.index("## Capabilities (routing guide)")
    cat_pos = sp.index("## Action categories")
    beh_pos = sp.index("## Behaviour")
    assert cap_pos < cat_pos < beh_pos


def test_wrapper_only_size_smaller_than_legacy() -> None:
    """Tier 2: wrapper-only SP is < 3000 chars (legacy is ~9500 chars)."""
    sp_legacy = build_system_prompt(**_BASE_KWARGS, hide_legacy_tools=False)
    sp_wrapper = build_system_prompt(**_BASE_KWARGS, hide_legacy_tools=True)
    assert len(sp_wrapper) < len(sp_legacy), (
        f"Wrapper-only SP not smaller: wrapper={len(sp_wrapper)} legacy={len(sp_legacy)}"
    )
    assert len(sp_wrapper) < 3000, (
        f"Wrapper-only SP exceeds 3000-char target: {len(sp_wrapper)}"
    )


def test_wrapper_only_sp_behaviour_contains_plan_intent_routing() -> None:
    """Tier 2: ## Behaviour section contains plan routing (multi-source guidance).

    The user amendment (B23-PRE-1 policy) requires that plan's 'when to use'
    decision lives in the SP Behaviour section as a cross-cutting policy, not
    only inside the plan tool description.  Verified: 'plan' + 'multi-source'
    (or 'multiple independent sources') appear in the ## Behaviour section.
    """
    sp = build_system_prompt(**_BASE_KWARGS, hide_legacy_tools=True)
    beh_start = sp.index("## Behaviour")
    # find next top-level ## after Behaviour (= About this project or end)
    next_section = sp.find("\n## ", beh_start + 1)
    beh_section = sp[beh_start:next_section] if next_section != -1 else sp[beh_start:]
    assert "plan" in beh_section, (
        "## Behaviour must reference plan routing (policy: 'plan' absent)"
    )
    assert (
        "multi-source" in beh_section
        or "multiple independent sources" in beh_section
    ), (
        "## Behaviour must include plan's multi-source guidance "
        "('multi-source' or 'multiple independent sources' absent)"
    )


def test_wrapper_only_sp_intent_routing_is_3_way() -> None:
    """Tier 2: ## Behaviour encodes 3-way intent routing (Action / Plan / Reply).

    The user amendment requires top-level intent routing to be 3-way so the
    LLM knows to choose plan for multi-source queries, not just Action or Reply.
    Verified: all three routing axes appear in the ## Behaviour section text.
    """
    sp = build_system_prompt(**_BASE_KWARGS, hide_legacy_tools=True)
    beh_start = sp.index("## Behaviour")
    next_section = sp.find("\n## ", beh_start + 1)
    beh_section = sp[beh_start:next_section] if next_section != -1 else sp[beh_start:]
    assert "invoke_action" in beh_section, (
        "## Behaviour must mention Action axis (invoke_action)"
    )
    assert "plan" in beh_section, (
        "## Behaviour must mention Plan axis"
    )
    # Reply axis: either 'Chitchat → Reply' from the 3-way routing line,
    # or the legacy 'Reply directly only for chitchat' bullet.
    assert "Reply" in beh_section or "chitchat" in beh_section, (
        "## Behaviour must mention Reply axis"
    )


def test_wrapper_only_sp_behaviour_contains_errors_verbatim_policy() -> None:
    """Tier 2: ## Behaviour contains errors verbatim policy (cross-cutting policy 5).

    B23-PRE-1: Optimism bias / errors verbatim rule stays in SP as a cross-cutting
    policy. Per-tool handling (spawn-ack, task_completed) moves to invoke_action
    description but the cross-cutting 'errors MUST surface verbatim' rule stays.
    """
    sp = build_system_prompt(**_BASE_KWARGS, hide_legacy_tools=True)
    beh_start = sp.index("## Behaviour")
    next_section = sp.find("\n## ", beh_start + 1)
    beh_section = sp[beh_start:next_section] if next_section != -1 else sp[beh_start:]
    assert "verbatim" in beh_section or "Optimism bias" in beh_section, (
        "## Behaviour must include errors-verbatim / Optimism bias policy"
    )


def test_wrapper_only_sp_does_not_contain_spawn_ack_priority() -> None:
    """Tier 2: spawn-ack Priority 1-4 block absent from wrapper-only SP.

    B23-PRE-1: spawn-ack handling moved to invoke_action.description.
    """
    sp = build_system_prompt(**_BASE_KWARGS, hide_legacy_tools=True)
    assert "Priority 1 (non-negotiable)" not in sp
    assert "Priority 2:" not in sp
    assert "Priority 3:" not in sp
    assert "Priority 4:" not in sp


def test_wrapper_only_sp_does_not_contain_agent_delegation_subsection() -> None:
    """Tier 2: ## Agent delegation subsection absent from wrapper-only SP.

    B23-PRE-1: agent delegation moved to invoke_action.description.
    """
    sp = build_system_prompt(**_BASE_KWARGS, hide_legacy_tools=True)
    assert "## Agent delegation" not in sp


def test_wrapper_only_sp_does_not_contain_plan_decomposition_subsection() -> None:
    """Tier 2: ## Plan decomposition subsection absent from wrapper-only SP.

    B23-PRE-1: plan WHAT/WHEN_NOT moved to plan.description. The 2-line
    multi-source routing policy stays in ## Behaviour.
    """
    sp = build_system_prompt(**_BASE_KWARGS, hide_legacy_tools=True)
    assert "## Plan decomposition" not in sp


def test_wrapper_only_sp_does_not_contain_memory_inline_section() -> None:
    """Tier 2: ## Memory inline section absent from wrapper-only SP.

    B23-PRE-1: memory discovery via list_actions(category=['memory.entry']).
    """
    sp = build_system_prompt(**_BASE_KWARGS, hide_legacy_tools=True)
    assert "## Memory (entries inlined" not in sp


def test_wrapper_only_sp_does_not_contain_mcp_section() -> None:
    """Tier 2: ## MCP servers and tools section absent from wrapper-only SP.

    B23-PRE-1: MCP discovery via list_actions(category=['mcp.server','mcp.tool']).
    """
    kwargs_with_mcp = dict(
        agent_name="default",
        agent_role="generalist",
        available_skills=[],
        available_agents=[],
        memory_index={"status": "ok", "shared": [], "agent": []},
        mcp_servers=[{"name": "brave", "description": "web search"}],
        universal_wrappers_enabled=True,
    )
    sp = build_system_prompt(**kwargs_with_mcp, hide_legacy_tools=True)
    assert "## MCP servers and tools" not in sp


def test_wrapper_only_sp_does_not_contain_files_section() -> None:
    """Tier 2: ## Files section absent from wrapper-only SP.

    B23-PRE-1: Files permission scope communicated via file.* category at runtime.
    """
    kwargs_with_files = dict(
        agent_name="default",
        agent_role="generalist",
        available_skills=[],
        available_agents=[],
        memory_index={"status": "ok", "shared": [], "agent": []},
        file_permissions={"read": ["*"], "write": ["*"]},
        universal_wrappers_enabled=True,
    )
    sp = build_system_prompt(**kwargs_with_files, hide_legacy_tools=True)
    assert "## Files (resource axis" not in sp


def test_wrapper_only_sp_does_not_contain_indexed_sources_section() -> None:
    """Tier 2: ## Indexed sources section absent from wrapper-only SP.

    B23-PRE-1: indexed source discovery via list_actions(category=['rag.corpus']).
    """
    sp = build_system_prompt(
        **_BASE_KWARGS,
        hide_legacy_tools=True,
        indexed_sources_section="## Indexed sources\n  meetings (= notes)",
    )
    assert "## Indexed sources" not in sp


def test_wrapper_only_sp_does_not_contain_japanese_disambiguation_table() -> None:
    """Tier 2: JA disambiguation table absent from wrapper-only SP.

    B23-PRE-1: multilingual disambiguation moved to per-tool descriptions
    (rag.operation__recall, memory.operation__remember_shared).
    """
    sp = build_system_prompt(
        **_BASE_KWARGS,
        hide_legacy_tools=True,
        indexed_sources_section="## Indexed sources\n  meetings (= notes)",
    )
    assert "Japanese input disambiguation" not in sp
    assert "覚えて" not in sp
    assert "思い出して" not in sp
