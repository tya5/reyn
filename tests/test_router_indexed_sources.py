"""Tier 2 tests — Router system prompt "Indexed sources" section (ADR-0033 UX gap fix A).

Verifies that SourceManifest.format_for_prompt() output is correctly injected
into the router system prompt, including the empty-state getting-started hint
and section ordering guarantees.
"""
from __future__ import annotations

import pytest

from reyn.chat.router_system_prompt import build_system_prompt
from reyn.index.source_manifest import SourceEntry, SourceManifest

# ---------------------------------------------------------------------------
# Helpers — minimal args for build_system_prompt
# ---------------------------------------------------------------------------

_EMPTY_MEMORY: dict = {"status": "not_found", "content": ""}

_SYNTHETIC_MEMORY_CONTENT = """\
# Memory Index (shared)
- [User role](user_role.md) — describes user's role

# Memory Index (agent: chat_20240101)
- [User pref](user_pref.md) — user preference
"""

_SYNTHETIC_MEMORY: dict = {"status": "ok", "content": _SYNTHETIC_MEMORY_CONTENT}


def _minimal_prompt(indexed_sources_section: str | None = None) -> str:
    """Build a minimal system prompt with the given indexed_sources_section."""
    return build_system_prompt(
        agent_name="chat",
        agent_role="general assistant",
        available_skills=[{"name": "review", "category": "general"}],
        available_agents=[],
        memory_index=_SYNTHETIC_MEMORY,
        indexed_sources_section=indexed_sources_section,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestEmptyStateHint:
    @pytest.mark.asyncio
    async def test_empty_manifest_format_for_prompt_contains_hint(self, tmp_path):
        """Tier 2: Empty SourceManifest.format_for_prompt() returns getting-started hint."""
        manifest = SourceManifest(tmp_path)
        rendered = await manifest.format_for_prompt()
        assert "No indexed sources yet" in rendered
        assert "reyn run index_docs" in rendered

    @pytest.mark.asyncio
    async def test_empty_manifest_hint_reaches_system_prompt(self, tmp_path):
        """Tier 2: Empty manifest hint is injected verbatim into router system prompt."""
        manifest = SourceManifest(tmp_path)
        section = await manifest.format_for_prompt()
        prompt = _minimal_prompt(indexed_sources_section=section)
        assert "No indexed sources yet" in prompt
        assert "reyn run index_docs" in prompt


class TestSourcesInPrompt:
    @pytest.mark.asyncio
    async def test_three_sources_appear_in_prompt(self, tmp_path):
        """Tier 2: System prompt lists all source names and chunk counts when 3 sources exist."""
        manifest = SourceManifest(tmp_path)
        await manifest.upsert(SourceEntry(
            name="memory", description="User notes", path=".reyn/memory/*.md",
            chunk_count=142,
        ))
        await manifest.upsert(SourceEntry(
            name="reyn_code", description="Reyn Python framework code",
            path="src/**/*.py", chunk_count=1247,
        ))
        await manifest.upsert(SourceEntry(
            name="reyn_docs", description="Reyn bundled mkdocs documentation",
            path="docs/**/*.md", chunk_count=89,
        ))
        section = await manifest.format_for_prompt()
        prompt = _minimal_prompt(indexed_sources_section=section)

        assert "memory" in prompt
        assert "reyn_code" in prompt
        assert "reyn_docs" in prompt
        assert "142 chunks" in prompt
        assert "1247 chunks" in prompt
        assert "89 chunks" in prompt

    @pytest.mark.asyncio
    async def test_sources_count_in_header(self, tmp_path):
        """Tier 2: Indexed sources header shows correct count."""
        manifest = SourceManifest(tmp_path)
        await manifest.upsert(SourceEntry(
            name="alpha", description="Alpha source", path="alpha/*.txt",
            chunk_count=10,
        ))
        await manifest.upsert(SourceEntry(
            name="beta", description="Beta source", path="beta/*.txt",
            chunk_count=20,
        ))
        section = await manifest.format_for_prompt()
        assert "## Indexed sources (2 available)" in section


class TestSectionOrdering:
    @pytest.mark.asyncio
    async def test_skills_before_memory_before_indexed_sources(self, tmp_path):
        """Tier 2: Section order — Skills < Memory < Indexed sources (string position).

        The router system prompt structure is:
          1. Identity / What you can do
          2. ## Skills
          3. ## Agents
          4. ## Memory   (← inlined recall store)
          5. ## Indexed sources  (← new: vector retrieval store)
          6. ## Behaviour
        """
        manifest = SourceManifest(tmp_path)
        await manifest.upsert(SourceEntry(
            name="my_src", description="My code", path="src/**/*.py",
            chunk_count=500,
        ))
        section = await manifest.format_for_prompt()
        prompt = _minimal_prompt(indexed_sources_section=section)

        pos_memory = prompt.index("## Memory")
        pos_indexed = prompt.index("## Indexed sources")
        pos_skills = prompt.index("## Skills")

        assert pos_skills < pos_memory, (
            "Skills section must appear before Memory section"
        )
        assert pos_memory < pos_indexed, (
            "Memory section must appear before Indexed sources section"
        )

    @pytest.mark.asyncio
    async def test_memory_before_indexed_sources_empty_state(self, tmp_path):
        """Tier 2: Memory < Indexed sources ordering holds in empty-source state."""
        manifest = SourceManifest(tmp_path)
        section = await manifest.format_for_prompt()
        prompt = _minimal_prompt(indexed_sources_section=section)

        pos_memory = prompt.index("## Memory")
        pos_indexed = prompt.index("## Indexed sources")

        assert pos_memory < pos_indexed, (
            "Memory section must appear before Indexed sources section (empty state)"
        )


class TestBackwardCompat:
    def test_section_absent_when_none(self):
        """Tier 2: indexed_sources_section=None means no Indexed sources block in prompt."""
        prompt = _minimal_prompt(indexed_sources_section=None)
        assert "## Indexed sources" not in prompt

    def test_no_section_by_default(self):
        """Tier 2: build_system_prompt omits Indexed sources when parameter not passed."""
        prompt = build_system_prompt(
            agent_name="chat",
            agent_role="assistant",
            available_skills=[],
            available_agents=[],
            memory_index=_EMPTY_MEMORY,
            # indexed_sources_section not passed — default is None
        )
        assert "## Indexed sources" not in prompt


class TestDeterministicOrder:
    @pytest.mark.asyncio
    async def test_sources_sorted_alphabetically_after_file_reload(self, tmp_path):
        """Tier 2: Sources are in alphabetical order when loaded from file.

        SourceManifest writes YAML with sort_keys=True. A fresh SourceManifest
        that loads from disk (= common restart scenario) returns sources in
        alphabetical order regardless of original insertion order.
        """
        manifest = SourceManifest(tmp_path)
        # Insert in non-alpha order to confirm file sorting
        await manifest.upsert(SourceEntry(
            name="zebra", description="Zebra source", path="z/*.txt",
            chunk_count=1,
        ))
        await manifest.upsert(SourceEntry(
            name="apple", description="Apple source", path="a/*.txt",
            chunk_count=2,
        ))
        await manifest.upsert(SourceEntry(
            name="mango", description="Mango source", path="m/*.txt",
            chunk_count=3,
        ))
        # Simulate restart: fresh manifest loads from disk (sorted by yaml)
        fresh = SourceManifest(tmp_path)
        section = await fresh.format_for_prompt()

        # All three appear
        assert "apple" in section
        assert "mango" in section
        assert "zebra" in section

        # After file reload: alphabetical order (yaml.safe_dump sort_keys=True)
        pos_apple = section.index("apple")
        pos_mango = section.index("mango")
        pos_zebra = section.index("zebra")
        assert pos_apple < pos_mango < pos_zebra, (
            "Sources should be listed alphabetically after file reload"
        )
