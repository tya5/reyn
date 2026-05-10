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


# ---------------------------------------------------------------------------
# B17-S5-3 fix: Vocab disambiguation — "Recall" intent rename + Behaviour rules
# ---------------------------------------------------------------------------


class TestVocabDisambiguationB17S53:
    """Tier 2: B17-S5-3 fix — 'recall' vocabulary collision.

    The intent label was renamed from 'Recall' to 'Memory access' to avoid
    colliding with the `recall` indexed-search tool (ADR-0033). Three
    disambiguation Behaviour rules are also pinned here.
    """

    def test_intent_label_is_memory_access_not_recall(self):
        """Tier 2: Intent axis uses 'Memory access' not 'Recall' for memory ops.

        B17-S5-3 fix: the word 'Recall' as an intent label caused the LLM to
        map user phrases like 'recall tool' to list_memory/read_memory_body
        instead of the indexed-search `recall` tool.
        """
        prompt = _minimal_prompt(indexed_sources_section=None)
        # New label must be present
        assert "Memory access" in prompt
        # Old label must NOT be present as an intent label (prevents regression)
        # Check the specific intent-label form "Recall — read persisted facts"
        assert "Recall — read persisted facts" not in prompt

    def test_recall_word_disambiguation_rule_present(self):
        """Tier 2: Behaviour section has explicit rule mapping 'recall' word
        to the indexed-search tool, NOT to memory retrieval tools.

        B17-S5-3 fix: without this rule, 100% of runs (5/5) mapped 'recall'
        to list_memory/read_memory_body instead of the `recall` tool.
        """
        prompt = _minimal_prompt(indexed_sources_section="## Indexed sources (0 available)\nNo indexed sources yet.")
        # Rule must disambiguate the word "recall"
        assert "recall" in prompt.lower()
        # Rule must reference the indexed-search tool path
        assert "list_memory" in prompt  # must appear as the contrasted alternative
        # Disambiguation rule must be present in Behaviour section
        assert "Do NOT map it to list_memory" in prompt

    def test_data_sources_disambiguation_rule_present(self):
        """Tier 2: Behaviour section has explicit rule that 'data sources'
        must list BOTH memory entries AND indexed sources.

        B17-S1-1 fix: without this rule, 100% of runs (3/3) answered 'data
        sources' with only memory layers (shared/agent), ignoring indexed
        sources entirely.
        """
        prompt = _minimal_prompt(indexed_sources_section="## Indexed sources (0 available)\nNo indexed sources yet.")
        assert "data sources" in prompt.lower()
        # Rule must mention that both layers need to be listed
        assert "BOTH" in prompt
        assert "Memory section" in prompt
        assert "Indexed sources" in prompt

    def test_search_docs_disambiguation_rule_present(self):
        """Tier 2: Behaviour section has rule directing 'search'/'find in docs'
        to the `recall` tool, not to list_memory/read_memory_body.
        """
        prompt = _minimal_prompt(indexed_sources_section="## Indexed sources (0 available)\nNo indexed sources yet.")
        assert "`recall`" in prompt
        assert "list_memory / read_memory_body" in prompt
        # The rule must contrast recall tool vs memory tools for search queries
        assert "Do NOT use list_memory" in prompt


# ---------------------------------------------------------------------------
# B17-S1-1 fix: Empty-state hint strengthening
# ---------------------------------------------------------------------------


class TestEmptyStateHintStrengthened:
    """Tier 2: B17-S1-1 fix — stronger empty-state indexed sources guidance.

    When 0 indexed sources are available and the user asks about data sources,
    the LLM must actively suggest `reyn run index_docs`, not fall back to
    describing memory as the only data source.
    """

    @pytest.mark.asyncio
    async def test_empty_state_behaviour_rule_present(self, tmp_path):
        """Tier 2: Behaviour section includes explicit instruction to suggest
        `reyn run index_docs` when 0 indexed sources and user asks about data
        sources.

        B17-S1-1 fix: empty-state hint existed in the Indexed sources section
        but was ignored by the LLM (3/3 runs mapped 'data sources' to memory).
        The fix adds an explicit Behaviour rule as a stronger forcing signal.
        """
        manifest = SourceManifest(tmp_path)
        section = await manifest.format_for_prompt()
        prompt = _minimal_prompt(indexed_sources_section=section)
        # Behaviour rule must reference the index_docs command
        assert "reyn run index_docs" in prompt
        # The rule must connect it to the 'data sources' query context
        assert "data" in prompt.lower()
        # Must discourage answering with memory-only (check the prohibition phrase)
        assert "Do NOT answer with memory-only" in prompt

    @pytest.mark.asyncio
    async def test_empty_state_behaviour_rule_absent_when_no_indexed_sources_section(self, tmp_path):
        """Tier 2: Empty-state Behaviour rule is NOT emitted when
        indexed_sources_section=None (backward-compat for non-chat paths).
        """
        prompt = _minimal_prompt(indexed_sources_section=None)
        # The empty-state enforcement rule is only injected when RAG is wired up.
        # Without indexed_sources_section, this rule must not appear in Behaviour.
        assert "reyn run index_docs" not in prompt

    @pytest.mark.asyncio
    async def test_when_asked_what_can_do_mentions_indexed_sources(self, tmp_path):
        """Tier 2: 'When asked what you can do' section mentions indexed sources
        when indexed_sources_section is provided.

        B17-S1-1 fix: the section previously mentioned only memory ('remember
        and recall facts via your memory'), omitting indexed sources entirely,
        which contributed to the memory-as-data-sources attractor.
        """
        manifest = SourceManifest(tmp_path)
        section = await manifest.format_for_prompt()
        prompt = _minimal_prompt(indexed_sources_section=section)
        # The capability list must mention indexed sources / recall tool
        assert "recall" in prompt.lower()
        assert "Indexed sources" in prompt

    def test_when_asked_what_can_do_no_indexed_mention_without_section(self):
        """Tier 2: Without indexed_sources_section, 'When asked what you can do'
        does NOT mention indexed sources (backward-compat; can't claim a
        capability that isn't wired up).
        """
        prompt = _minimal_prompt(indexed_sources_section=None)
        # No mention of indexed search tool capability when RAG not wired
        assert "search indexed document sources" not in prompt
