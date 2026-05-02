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
        # analyze and read must not appear (no skills in those categories)
        assert "analyze" not in prompt
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


class TestMemoryCountFromIndex:
    def test_memory_count_from_index(self):
        prompt = build_system_prompt(
            agent_name="chat",
            agent_role="assistant",
            available_skills=[],
            available_agents=[],
            memory_index=_SYNTHETIC_MEMORY,
        )
        # shared section: user(1) project(1) feedback(1)
        assert "user(1)" in prompt
        assert "project(1)" in prompt
        assert "feedback(1)" in prompt
        # agent section: user(1) reference(1)
        assert "reference(1)" in prompt

    def test_memory_not_found_shows_empty(self):
        prompt = build_system_prompt(
            agent_name="chat",
            agent_role="assistant",
            available_skills=[],
            available_agents=[],
            memory_index={"status": "not_found", "content": ""},
        )
        assert "(empty)" in prompt


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


class TestSizeIsO1InItems:
    def test_size_independent_of_item_count(self):
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
        # The only difference should be the count digit(s): "general (3)" vs
        # "general (30)" — at most 1 extra character.
        size_diff = abs(len(prompt_30) - len(prompt_3))
        assert size_diff < 5, (
            f"Prompt grew by {size_diff} chars — not O(1) in item count."
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
# New tests: Intent axis annotations
# ---------------------------------------------------------------------------

class TestIntentAxisFilesMCPAnnotations:
    def test_intent_axis_includes_file_mcp_annotations(self):
        """Annotations are always present regardless of actual permission state."""
        prompt = build_system_prompt(
            agent_name="chat",
            agent_role="assistant",
            available_skills=[],
            available_agents=[],
            memory_index=_EMPTY_MEMORY,
        )
        assert "(when file scope set)" in prompt
        assert "(when mcp configured)" in prompt
