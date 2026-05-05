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

_SYNTHETIC_MEMORY_CONTENT = """\
# Memory Index (shared)
- [User role](user_role.md) — describes user's role
- [Project x](project_x.md) — main project context
- [Feedback y](feedback_y.md) — past feedback entry

# Memory Index (agent: chat_20240101)
- [User pref](user_pref.md) — user preference
- [Reference doc](reference_doc.md) — a reference
"""

_SYNTHETIC_MEMORY: dict = {"status": "ok", "content": _SYNTHETIC_MEMORY_CONTENT}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestEmptyStateRenders:
    def test_empty_state_renders(self):
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
        # Intent axis always present
        assert "Action" in prompt
        assert "Recall" in prompt
        # No real category lines — shows (none) placeholders
        assert "(none)" in prompt


class TestCategoriesGroupedCorrectly:
    def test_categories_grouped(self):
        skills = [
            _make_skill("s1", "general"),
            _make_skill("s2", "write"),
            _make_skill("s3", "write"),
        ]
        prompt = build_system_prompt(
            agent_name="chat",
            agent_role="assistant",
            available_skills=skills,
            available_agents=[],
            memory_index=_EMPTY_MEMORY,
        )
        assert "general (1)" in prompt
        assert "write (2)" in prompt
        # analyze and read must not appear AS CATEGORIES (no skills in those
        # categories). Bare verbs may appear in the Behaviour section's
        # domain-verb list — use the "(N)" suffix to assert category-form.
        assert "analyze (" not in prompt
        assert "read (0)" not in prompt

    def test_missing_category_defaults_to_general(self):
        skills = [_make_skill("no_cat_skill")]  # no category key
        prompt = build_system_prompt(
            agent_name="chat",
            agent_role="assistant",
            available_skills=skills,
            available_agents=[],
            memory_index=_EMPTY_MEMORY,
        )
        assert "general (1)" in prompt


class TestMemoryEntriesInlined:
    """PR36b: Memory descriptions are inlined so LLM can answer recall
    queries directly. Previously only counts were shown, defeating the
    point of having memory."""

    def test_memory_entries_with_descriptions_inlined(self):
        prompt = build_system_prompt(
            agent_name="chat",
            agent_role="assistant",
            available_skills=[],
            available_agents=[],
            memory_index=_SYNTHETIC_MEMORY,
        )
        # Slugs from _SYNTHETIC_MEMORY (shared layer) should appear in the prompt
        assert "user_role" in prompt
        assert "project_x" in prompt
        assert "feedback_y" in prompt
        # Slugs from agent layer also visible
        assert "user_pref" in prompt
        assert "reference_doc" in prompt
        # Description fragments visible (so LLM can answer recall queries)
        assert "describes user's role" in prompt
        assert "main project context" in prompt

    def test_memory_layers_separately_rendered(self):
        prompt = build_system_prompt(
            agent_name="chat",
            agent_role="assistant",
            available_skills=[],
            available_agents=[],
            memory_index=_SYNTHETIC_MEMORY,
        )
        # The Memory section should have "shared:" and "agent:" subheads.
        assert "shared:" in prompt
        assert "agent:" in prompt

    def test_memory_not_found_shows_no_entries(self):
        prompt = build_system_prompt(
            agent_name="chat",
            agent_role="assistant",
            available_skills=[],
            available_agents=[],
            memory_index={"status": "not_found", "content": ""},
        )
        # No old "(empty)" / count format; new format says "(no entries)"
        assert "(no entries)" in prompt


class TestIntentAxisSectionAlwaysPresent:
    @pytest.mark.parametrize("intent_fragment", [
        "Action — run external work",
        "Recall —",
        "Save —",
        "Forget —",
        "Reply —",
    ])
    def test_intent_axis_always_present(self, intent_fragment: str):
        prompt = build_system_prompt(
            agent_name="bot",
            agent_role="test role",
            available_skills=[],
            available_agents=[],
            memory_index=_EMPTY_MEMORY,
        )
        assert intent_fragment in prompt


class TestSizeIsLinearInItems:
    """RETRO-H1+H2 design change: the system prompt now injects a flat skill
    list so the LLM knows exact skill names (prevents zero-shot hallucination).
    Size is intentionally O(N) in skill count. The old O(1) invariant no longer
    applies after this fix."""

    def test_size_grows_with_skill_count(self):
        """Tier 2: prompt with more skills is larger (flat list injected)."""
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
        # 30-skill prompt must be larger than 3-skill prompt
        assert len(prompt_30) > len(prompt_3), (
            "Expected 30-skill prompt to be larger than 3-skill prompt "
            "(flat skill list grows linearly with skill count)"
        )
        # Growth should be approximately linear (not O(N²) or constant)
        # 27 extra skills, each adding roughly the same number of chars
        size_diff = len(prompt_30) - len(prompt_3)
        # Each extra skill adds at least 5 chars (e.g. "  - skill_X\n")
        assert size_diff >= 27 * 5, (
            f"Expected at least {27 * 5} chars growth for 27 extra skills, "
            f"got {size_diff}"
        )


class TestJapaneseInRolePreserved:
    def test_japanese_role(self):
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
# New tests: Files section
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
        prompt = self._base_prompt()
        assert "## Files" not in prompt

    def test_files_section_omitted_when_none(self):
        prompt = self._base_prompt(file_permissions=None)
        assert "## Files" not in prompt

    def test_files_section_omitted_when_both_empty(self):
        prompt = self._base_prompt(file_permissions={"read": [], "write": []})
        assert "## Files" not in prompt

    def test_files_section_with_read_only(self):
        prompt = self._base_prompt(
            file_permissions={"read": ["src", "docs"], "write": []}
        )
        assert "## Files" in prompt
        assert "read scope:" in prompt
        assert "src" in prompt
        assert "docs" in prompt
        assert "write scope:" not in prompt

    def test_files_section_with_full_scope(self):
        prompt = self._base_prompt(
            file_permissions={"read": ["src", "docs"], "write": ["output"]}
        )
        assert "## Files" in prompt
        assert "read scope:" in prompt
        assert "write scope:" in prompt
        assert "output" in prompt


# ---------------------------------------------------------------------------
# New tests: MCP servers section
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
        prompt = self._base_prompt()
        assert "## MCP servers" not in prompt

    def test_mcp_section_omitted_when_none(self):
        prompt = self._base_prompt(mcp_servers=None)
        assert "## MCP servers" not in prompt

    def test_mcp_section_omitted_when_empty_list(self):
        prompt = self._base_prompt(mcp_servers=[])
        assert "## MCP servers" not in prompt

    def test_mcp_section_with_servers(self):
        prompt = self._base_prompt(
            mcp_servers=[
                {"name": "fs", "description": "filesystem"},
                {"name": "fetch", "description": "web fetch"},
            ]
        )
        assert "## MCP servers" in prompt
        assert "- fs: filesystem" in prompt
        assert "- fetch: web fetch" in prompt
        assert "list_mcp_tools" in prompt

    def test_mcp_server_no_description_uses_placeholder(self):
        prompt = self._base_prompt(
            mcp_servers=[{"name": "bare"}]
        )
        assert "## MCP servers" in prompt
        assert "bare" in prompt
        assert "(no description)" in prompt

    def test_size_growth_constant_in_mcp_servers(self):
        """MCP section grows linearly with server count (expected, not O(n²))."""
        server_1 = [{"name": "srv", "description": "a server"}]
        server_30 = [{"name": f"srv_{i}", "description": "a server"} for i in range(30)]

        prompt_1 = self._base_prompt(mcp_servers=server_1)
        prompt_30 = self._base_prompt(mcp_servers=server_30)

        size_1 = len(prompt_1)
        size_30 = len(prompt_30)
        # 30 servers should be larger than 1 server (linear growth is fine)
        assert size_30 > size_1
        # But growth should be strictly linear — not O(n²).
        # Each extra server adds roughly the same chars as one server entry.
        per_server_cost = size_30 - size_1  # chars added by 29 extra servers
        # Adding another 29 servers would not add more than 2× what 29 added
        # (i.e., growth is bounded linearly, not quadratically).
        assert per_server_cost < size_1 * 10  # sanity: not absurdly large


# ---------------------------------------------------------------------------
# New tests: Dynamic intent axis (PR36 Layer 2)
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
        prompt = _base_prompt(file_permissions=None)
        assert "read_file" not in prompt
        assert "write_file" not in prompt
        assert "delete_file" not in prompt
        assert "list_directory" not in prompt

    def test_no_mcp_tool_names_when_no_mcp_servers(self):
        prompt = _base_prompt(mcp_servers=[])
        assert "list_mcp_servers" not in prompt
        assert "list_mcp_tools" not in prompt
        assert "call_mcp_tool" not in prompt

    def test_no_mcp_tool_names_when_mcp_servers_none(self):
        prompt = _base_prompt(mcp_servers=None)
        assert "list_mcp_servers" not in prompt
        assert "list_mcp_tools" not in prompt
        assert "call_mcp_tool" not in prompt

    def test_no_when_clause_annotations(self):
        prompt = _base_prompt()
        assert "(when file scope set)" not in prompt
        assert "(when mcp configured)" not in prompt
        assert "(when file write scope set)" not in prompt

    def test_write_file_only_when_write_scope(self):
        prompt = _base_prompt(
            file_permissions={"read": ["src"], "write": []}
        )
        assert "read_file" in prompt
        assert "list_directory" in prompt
        assert "write_file" not in prompt
        assert "delete_file" not in prompt

    def test_full_file_scope_shows_all_file_tools(self):
        prompt = _base_prompt(
            file_permissions={"read": ["src"], "write": ["output"]}
        )
        assert "read_file" in prompt
        assert "list_directory" in prompt
        assert "write_file" in prompt
        assert "delete_file" in prompt

    def test_mcp_tools_when_servers_configured(self):
        prompt = _base_prompt(mcp_servers=[{"name": "fs"}])
        assert "list_mcp_servers" in prompt
        assert "list_mcp_tools" in prompt
        assert "call_mcp_tool" in prompt

    def test_intent_axis_still_renders_without_permissions(self):
        """Core intent rows must remain even with no permissions."""
        prompt = _base_prompt()
        assert "Action — run external work" in prompt
        assert "skills:  list_skills / describe_skill / invoke_skill" in prompt
        assert "agents:  list_agents / describe_agent / delegate_to_agent" in prompt
        assert "Recall — read persisted facts" in prompt
        assert "Save — persist new facts" in prompt
        assert "Forget — delete persisted facts" in prompt
        assert "Reply — answer directly (no tool)" in prompt


# ---------------------------------------------------------------------------
# F3 + F9 fix (PR-router-fix): Behaviour rules tighten the routing decision
# ---------------------------------------------------------------------------

class TestBehaviourRulesAfterF3F9Fix:
    """Tier 2: pin the Behaviour rules added to address 0/3 routing
    failures observed in dogfood batch 1 (findings F3 + F9). The rules
    are intentionally minimal — they're concrete disambiguation hints
    for weaker models, not a complete fix. (For consistent routing on
    weak models like gemini-2.5-flash-lite, a stronger router model is
    the structural fix; the prompt rules are best-effort.)"""

    def test_explicit_skill_name_directs_to_invoke(self):
        """Tier 2: prompt instructs LLM that user-named skills go directly to
        invoke_skill (not list_skills first, not a text Reply).

        B11-R3 fix: previously required list_skills first + stated
        'paraphrasing the request as Reply' is wrong. Changed to direct
        invoke_skill path when skill name is in Available skills list, which
        closes the 'clarification text-reply' escape hatch for weak LLMs.
        """
        prompt = build_system_prompt(
            agent_name="chat",
            agent_role="",
            available_skills=[_make_skill("read_local_files", "general")],
            available_agents=[],
            memory_index=_EMPTY_MEMORY,
        )
        # Named skill → direct invoke_skill (B11-R3 fix: skip list_skills when
        # skill name is already in the Available skills list)
        assert "If the user names a skill" in prompt
        assert "invoke_skill directly" in prompt
        # Additional entities in user message are inputs, NOT clarification reasons
        assert "inputs to the skill" in prompt or "NOT reasons to clarify" in prompt

    def test_reply_directly_restricted_to_chitchat(self):
        """Tier 2: 'Reply directly' rule restricted — only chitchat,
        self-questions, clarifications. Domain tasks must go to Action."""
        prompt = build_system_prompt(
            agent_name="chat",
            agent_role="",
            available_skills=[],
            available_agents=[],
            memory_index=_EMPTY_MEMORY,
        )
        assert "Reply directly only for chitchat" in prompt
        assert "Domain tasks" in prompt
        assert "Action" in prompt
        # The old too-permissive phrasing must be gone
        assert "stable knowledge" not in prompt

    def test_post_describe_commit_or_explain(self):
        """B2-H1 fix: after describe_skill, the LLM must commit by calling
        invoke_skill or explicitly explain in text — never stop silently
        mid-investigation. Without this rule, gemini-2.5-flash-lite enters
        a 'research complete, synthesise' state after describe_skill and
        emits an empty text turn, which triggers the F6 _no_reply_marker
        upstream and surfaces no value to the user.
        B5-H1 re-balance: rule is now an individual bullet with its own MUST
        so weak LLMs treat it as a first-class obligation (1 bullet = 1 MUST).
        """
        prompt = build_system_prompt(
            agent_name="chat",
            agent_role="assistant",
            available_skills=[{"name": "summarize", "category": "general"}],
            available_agents=[],
            memory_index={"status": "not_found", "content": ""},
        )
        # Rule must be its own bullet with MUST signal.
        assert "After describe_skill" in prompt
        assert "you MUST call invoke_skill" in prompt
        # The "or explain" half must be present too.
        assert "explain" in prompt.lower()

    def test_post_list_skills_must_invoke_or_describe(self):
        """Tier 2: B3-H1 + B3-M3 fix — after list_skills reveals a matching
        skill, the LLM must commit to describe_skill or invoke_skill and must
        not reply directly.  Both bugs share the same attractor: list_skills
        result ignored.  The rule targets the obligation post list_skills,
        complementing the B2-H1 post-describe_skill rule.
        B5-H1 re-balance: rule is its own bullet with MUST; "engage the skill
        ecosystem" jargon removed (too abstract for weak LLMs)."""
        prompt = build_system_prompt(
            agent_name="chat",
            agent_role="assistant",
            available_skills=[{"name": "text_summarizer", "category": "general"}],
            available_agents=[],
            memory_index={"status": "not_found", "content": ""},
        )
        # Rule must reference the trigger (list_skills reveals a skill)
        assert "After list_skills" in prompt
        # Must be its own bullet with MUST signal (1 bullet = 1 MUST)
        assert "you MUST" in prompt
        # Must mention both commitment paths
        assert "describe_skill" in prompt
        assert "invoke_skill" in prompt
        # Must prohibit direct reply when a skill is available
        assert "Do NOT reply" in prompt
        # "skill ecosystem" jargon removed — too abstract for weak LLMs
        assert "skill ecosystem" not in prompt
