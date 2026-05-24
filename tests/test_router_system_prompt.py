"""Tests for the PR35 router system prompt builder."""
from __future__ import annotations

import pytest

from reyn.chat.router_system_prompt import build_system_prompt

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_skill(name: str, category: str | None = None) -> dict:
    d: dict = {"name": name, "description": f"Does {name}"}
    if category is not None:
        d["category"] = category
    return d


def _make_agent(name: str, cluster: str | None = None) -> dict:
    d: dict = {"name": name, "role": f"Agent {name}"}
    if cluster is not None:
        d["cluster"] = cluster
    return d


_EMPTY_MEMORY: dict = {"status": "not_found", "content": ""}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestEmptyStateRenders:
    def test_empty_state_renders(self):
        """Tier 2: build_system_prompt returns a non-empty string with required sections."""
        prompt = build_system_prompt(
            agent_name="chat",
            agent_role="general assistant",
            available_skills=[],
            available_agents=[],
            memory_index=_EMPTY_MEMORY,
        )
        assert isinstance(prompt, str)
        assert len(prompt) > 0
        # Header must be present
        assert "Role: chat router for agent chat" in prompt
        # Routing guide section present
        assert "## Capabilities (routing guide)" in prompt
        # Behaviour section present
        assert "## Behaviour" in prompt


class TestSizeIsConstantInItems:
    """Category-only retry (2026-05-07): SP size is **O(1)** in skill count.

    Inverts the previous TestSizeIsLinearInItems contract. Skills are no
    longer enumerated in the SP — only a category-level pointer + count.
    Industry-aligned per Anthropic Tool Search Tool / OpenAI namespaces /
    MCP-Zero hierarchical patterns.

    Hallucination defense moved to the schema enum (= invoke_skill rejects
    unknown name); see test_invoke_skill_name_enum_matches_skill_list in
    test_router_invoke_skill_enum.py.
    """

    def test_size_constant_in_skill_count(self):
        """Tier 2: SP size is independent of skill count under category-only.

        With 3 skills vs 30 skills, the SP must be the same size modulo
        the count digits ("3 available" vs "30 available" = 1 char diff).
        """
        skills_3 = [_make_skill(f"skill_{i}", "general") for i in range(3)]
        skills_30 = [_make_skill(f"skill_{i}", "general") for i in range(30)]

        prompt_3 = build_system_prompt(
            agent_name="chat",
            agent_role="assistant",
            available_skills=skills_3,
            available_agents=[],
            memory_index=_EMPTY_MEMORY,
        )
        prompt_30 = build_system_prompt(
            agent_name="chat",
            agent_role="assistant",
            available_skills=skills_30,
            available_agents=[],
            memory_index=_EMPTY_MEMORY,
        )
        # In wrapper-only path the SP has no skill enumeration at all;
        # size must be identical regardless of skill count.
        size_diff = abs(len(prompt_30) - len(prompt_3))
        assert size_diff <= 5, (
            f"Expected SP to be O(1) in skill count under category-only retry; "
            f"got size diff {size_diff} between N=3 ({len(prompt_3)}) and "
            f"N=30 ({len(prompt_30)}). Difference must stay within count-digits."
        )


class TestJapaneseInRolePreserved:
    def test_japanese_role(self):
        """Tier 2: Japanese characters in agent_role are preserved verbatim in the system prompt."""
        role = "日本語エージェントの役割説明"
        prompt = build_system_prompt(
            agent_name="jp_bot",
            agent_role=role,
            available_skills=[],
            available_agents=[],
            memory_index=_EMPTY_MEMORY,
        )
        assert role in prompt
        assert "jp_bot" in prompt


# ---------------------------------------------------------------------------
# Files section: omitted in wrapper-only path
# ---------------------------------------------------------------------------

class TestFilesSection:
    def _base_prompt(self, **kwargs) -> str:
        return build_system_prompt(
            agent_name="chat",
            agent_role="assistant",
            available_skills=[],
            available_agents=[],
            memory_index=_EMPTY_MEMORY,
            **kwargs,
        )

    def test_files_section_omitted_when_no_permissions(self):
        """Tier 2: ## Files section absent in wrapper-only path (no file_permissions)."""
        prompt = self._base_prompt()
        assert "## Files" not in prompt

    def test_files_section_omitted_when_none(self):
        """Tier 2: ## Files section absent in wrapper-only path (file_permissions=None)."""
        prompt = self._base_prompt(file_permissions=None)
        assert "## Files" not in prompt

    def test_files_section_omitted_when_both_empty(self):
        """Tier 2: ## Files section absent in wrapper-only path (empty permissions)."""
        prompt = self._base_prompt(file_permissions={"read": [], "write": []})
        assert "## Files" not in prompt

    def test_files_section_omitted_even_with_read_permissions(self):
        """Tier 2: ## Files section absent in wrapper-only path even when permissions set.

        Phase 6 cleanup: files section removed from SP — discovery goes through
        list_actions(category=['file']) at runtime.
        """
        prompt = self._base_prompt(
            file_permissions={"read": ["src", "docs"], "write": []}
        )
        assert "## Files" not in prompt

    def test_files_section_omitted_with_full_scope(self):
        """Tier 2: ## Files section absent in wrapper-only path with full scope."""
        prompt = self._base_prompt(
            file_permissions={"read": ["src", "docs"], "write": ["output"]}
        )
        assert "## Files" not in prompt


# ---------------------------------------------------------------------------
# MCP servers section: omitted in wrapper-only path
# ---------------------------------------------------------------------------

class TestMCPSection:
    def _base_prompt(self, **kwargs) -> str:
        return build_system_prompt(
            agent_name="chat",
            agent_role="assistant",
            available_skills=[],
            available_agents=[],
            memory_index=_EMPTY_MEMORY,
            **kwargs,
        )

    def test_mcp_section_omitted_when_no_servers(self):
        """Tier 2: ## MCP servers section absent in wrapper-only path."""
        prompt = self._base_prompt()
        assert "## MCP servers" not in prompt

    def test_mcp_section_omitted_when_none(self):
        """Tier 2: ## MCP servers section absent in wrapper-only path (None)."""
        prompt = self._base_prompt(mcp_servers=None)
        assert "## MCP servers" not in prompt

    def test_mcp_section_omitted_when_empty_list(self):
        """Tier 2: ## MCP servers section absent in wrapper-only path (empty list)."""
        prompt = self._base_prompt(mcp_servers=[])
        assert "## MCP servers" not in prompt

    def test_mcp_section_omitted_with_servers(self):
        """Tier 2: ## MCP servers section absent in wrapper-only path even with servers.

        Phase 6 cleanup: MCP section removed from SP — discovery goes through
        list_actions(category=['mcp.server','mcp.tool']) at runtime.
        """
        prompt = self._base_prompt(
            mcp_servers=[
                {"name": "fs", "description": "filesystem"},
                {"name": "fetch", "description": "web fetch"},
            ]
        )
        assert "## MCP servers" not in prompt


# ---------------------------------------------------------------------------
# Dynamic tool names: absent in wrapper-only path
# ---------------------------------------------------------------------------

def _base_prompt(**kwargs) -> str:
    return build_system_prompt(
        agent_name="chat",
        agent_role="assistant",
        available_skills=[],
        available_agents=[],
        memory_index=_EMPTY_MEMORY,
        **kwargs,
    )


class TestIntentAxisDynamic:
    def test_no_file_tool_names_when_no_file_permissions(self):
        """Tier 2: file-class tools absent from the prompt without an
        explicit `file.*` declaration.

        Phase 6 cleanup: per-tool SP sections removed; discovery via
        list_actions(category=['file']) replaces SP enumeration.
        """
        prompt = _base_prompt(file_permissions=None)
        assert "read_file" not in prompt
        assert "write_file" not in prompt
        assert "delete_file" not in prompt
        assert "list_directory" not in prompt

    def test_no_mcp_tool_names_when_no_mcp_servers(self):
        """Tier 2: MCP tools absent from wrapper-only path SP."""
        prompt = _base_prompt(mcp_servers=[])
        assert "list_mcp_servers" not in prompt
        assert "list_mcp_tools" not in prompt
        assert "call_mcp_tool" not in prompt

    def test_no_mcp_tool_names_when_mcp_servers_none(self):
        """Tier 2: MCP tools absent from wrapper-only path SP (None)."""
        prompt = _base_prompt(mcp_servers=None)
        assert "list_mcp_servers" not in prompt
        assert "list_mcp_tools" not in prompt
        assert "call_mcp_tool" not in prompt

    def test_no_when_clause_annotations(self):
        """Tier 2: no conditional annotations in wrapper-only SP."""
        prompt = _base_prompt()
        assert "(when file scope set)" not in prompt
        assert "(when mcp configured)" not in prompt
        assert "(when file write scope set)" not in prompt

    def test_intent_axis_still_renders_without_permissions(self):
        """Tier 2: Core routing rules remain even with no permissions.

        Phase 6 cleanup: intent-axis row format removed; routing intent
        encoded in Behaviour section via invoke_action vocabulary.
        """
        prompt = _base_prompt()
        assert "ROUTING RULE (ABSOLUTE)" in prompt
        assert "invoke_action" in prompt
        assert "NO clarifying questions" in prompt
        assert "NO text replies" in prompt


# ---------------------------------------------------------------------------
# Behaviour rules (F3 + F9 fix): still present under invoke_action vocab
# ---------------------------------------------------------------------------

class TestBehaviourRulesAfterF3F9Fix:
    """Tier 2: pin the Behaviour rules that remain after Phase 6 SP simplification.

    B23-PRE-1 SP role-separation moved per-tool flow details (post-list MUST,
    post-describe MUST, spawn-ack, task_completed, delegation) to tool
    descriptions. The SP Behaviour section retains only cross-cutting policies.
    """

    def test_reply_directly_restricted_to_chitchat(self):
        """Tier 2: 'Reply directly' rule restricted — only chitchat.
        Domain tasks must go to invoke_action."""
        prompt = build_system_prompt(
            agent_name="chat",
            agent_role="",
            available_skills=[],
            available_agents=[],
            memory_index=_EMPTY_MEMORY,
        )
        assert "Chitchat" in prompt
        assert "invoke_action" in prompt
        assert "Domain task" in prompt

    def test_v3_absolute_routing_rule_present(self):
        """Tier 2: B13-R3 V3 wording — ABSOLUTE routing rule block is present
        in the Behaviour section with the required components.
        """
        prompt = build_system_prompt(
            agent_name="chat",
            agent_role="assistant",
            available_skills=[_make_skill("skill_improver", "general")],
            available_agents=[],
            memory_index=_EMPTY_MEMORY,
        )
        # ABSOLUTE rule framing must be present
        assert "ROUTING RULE (ABSOLUTE)" in prompt
        # Core imperative: call invoke_action immediately
        assert "invoke_action" in prompt
        # Explicit prohibitions
        assert "NO clarifying questions" in prompt
        assert "NO text replies" in prompt


class TestPostInvokeSkillNarrationGuidance:
    """Tier 2: FP-0034 B23-PRE-1 — spawn-ack and completion-narration content
    moved from SP to invoke_action.description. The SP Behaviour section retains
    only the cross-cutting errors/optimism-bias policy.

    The per-tool content (Priority 1 /tasks MUST, task_completed handling,
    anti-fabrication) is validated in test_tool_description_role_separation.py.
    """

    def test_anti_optimism_cross_cutting_policy_present(self):
        """Tier 2: SP Behaviour has cross-cutting anti-optimism policy.

        Errors MUST surface verbatim is a cross-cutting rule that applies
        regardless of which tool was most recently called.
        """
        prompt = build_system_prompt(
            agent_name="chat",
            agent_role="assistant",
            available_skills=[_make_skill("any_skill", "general")],
            available_agents=[],
            memory_index=_EMPTY_MEMORY,
        )
        assert "Errors MUST surface verbatim" in prompt
        assert "Optimism bias" in prompt
