"""Tier 1/2 tests — Router system prompt and the (removed) "Indexed sources" wiring.

Two orthogonal things live here:

- ``SourceManifest.format_for_prompt()`` output correctness (Tier 2): the
  manifest renders the empty-state getting-started hint and lists sources with
  chunk counts. This is manifest behaviour, independent of the SP.

- The absence of any "## Indexed sources" section in the wrapper-only SP, plus
  the absence of the ``indexed_sources_section`` parameter on
  ``build_system_prompt`` (Tier 1 contract). B23-PRE-1 dropped the section from
  the wrapper-only path; #3025 then removed the vestigial parameter and the
  per-turn ``SourceManifest.format_for_prompt()`` prefetch that fed it (the
  rendered string was accepted and discarded every turn). Corpus discovery is
  the ``list_rag_sources`` verb (#3026), not the SP.
"""
from __future__ import annotations

import inspect

import pytest

import reyn.runtime.router_system_prompt as router_system_prompt
from reyn.data.index.source_manifest import SourceEntry, SourceManifest
from reyn.runtime.router_system_prompt import build_system_prompt
from reyn.tools.schemes._universal_sp import build_universal_tool_use_slots

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


def _default_slots() -> "dict[str, str]":
    return build_universal_tool_use_slots(
        # #1977: these tests assert the WRAPPER-SP (invoke_action routing) — build
        # with wrappers ON to match that intent (pre-#1977 the wrappers-off SP
        # leaked the vocab, masking this flag). ON output is byte-identical.
        universal_wrappers_enabled=True,
        search_actions_enabled=True,
        discovery_mandate=False,
        has_hot_list_aliases=False,
        non_interactive=False,
    )


def _minimal_prompt() -> str:
    """Build a minimal wrapper-only system prompt.

    #1627 Stage 4: tool_use_sp slot-map required for tool-use SP content.
    """
    return build_system_prompt(
        agent_name="chat",
        agent_role="general assistant",
        available_agents=[],
        memory_index=_SYNTHETIC_MEMORY,
        tool_use_sp=_default_slots(),
    )


# ---------------------------------------------------------------------------
# Tests — SourceManifest.format_for_prompt() correctness
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
    async def test_manifest_hint_text_absent_from_wrapper_sp(self, tmp_path):
        """Tier 2: the manifest's rendered section does not appear in the wrapper-only SP.

        #3025: the SP has no "## Indexed sources" section, and even the distinctive
        empty-state hint text the manifest renders is absent from the SP — corpus
        discovery is the list_rag_sources verb (#3026), never the prompt.
        """
        manifest = SourceManifest(tmp_path)
        section = await manifest.format_for_prompt()
        assert "No indexed sources yet" in section  # sanity: manifest DID render it
        prompt = _minimal_prompt()
        assert "## Indexed sources" not in prompt
        assert "No indexed sources yet" not in prompt


class TestSourcesInPrompt:
    @pytest.mark.asyncio
    async def test_three_sources_in_manifest_format(self, tmp_path):
        """Tier 2: SourceManifest.format_for_prompt() lists all source names and chunk counts."""
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

        assert "memory" in section
        assert "reyn_code" in section
        assert "reyn_docs" in section
        assert "142 chunks" in section
        assert "1247 chunks" in section
        assert "89 chunks" in section

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


class TestNoIndexedSourcesSection:
    def test_section_absent_from_wrapper_sp(self):
        """Tier 2: the wrapper-only SP has no ## Indexed sources block."""
        prompt = _minimal_prompt()
        assert "## Indexed sources" not in prompt

    def test_no_section_with_empty_memory(self):
        """Tier 2: build_system_prompt omits Indexed sources regardless of memory state."""
        prompt = build_system_prompt(
            agent_name="chat",
            agent_role="assistant",
            available_agents=[],
            memory_index=_EMPTY_MEMORY,
        )
        assert "## Indexed sources" not in prompt


class TestDeadParamAndHelperRemoved:
    """Tier 1 (Contract): the discarded indexed_sources_section wiring is gone.

    #3025 removed the parameter build_system_prompt accepted-and-discarded, the
    per-turn SourceManifest.format_for_prompt() prefetch that fed it, and the
    caller-less _render_memory helper. These contract assertions go RED if any
    of that dead wiring is reintroduced (a re-added parameter would once again
    let a caller pay to build a section nothing renders).
    """

    def test_build_system_prompt_has_no_indexed_sources_param(self):
        """Tier 1: build_system_prompt exposes no indexed_sources_section parameter."""
        params = inspect.signature(build_system_prompt).parameters
        assert "indexed_sources_section" not in params

    def test_render_memory_helper_removed(self):
        """Tier 1: the caller-less _render_memory helper no longer exists."""
        assert not hasattr(router_system_prompt, "_render_memory")


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
# B17-S5-3 fix: Vocab disambiguation — wrapper-only SP
# ---------------------------------------------------------------------------


class TestVocabDisambiguationB17S53:
    """Tier 2: B17-S5-3 fix — 'recall' vocabulary collision.

    Phase 6 cleanup: intent axis and Behaviour disambiguation rules removed
    from SP (moved to tool descriptions). SP no longer contains
    list_memory / read_memory_body references. Tests updated to verify
    the correct absence of the old intent-label and the wrapper-only SP
    routing vocabulary.
    """

    def test_intent_label_is_not_recall_in_sp(self):
        """Tier 2: SP does not use 'Recall' as an intent axis label.

        B17-S5-3 fix: wrapper-only SP uses invoke_action routing vocabulary
        (no intent-axis labels). 'Recall — read persisted facts' must be absent.
        """
        prompt = _minimal_prompt()
        # Old intent label must NOT be present
        assert "Recall — read persisted facts" not in prompt
        # Also the corrected label form should not be present (intent axis removed)
        assert "Memory access — read persisted facts" not in prompt

    def test_sp_uses_invoke_action_routing(self):
        """Tier 2: Wrapper-only SP routes all actions through invoke_action."""
        prompt = _minimal_prompt()
        assert "invoke_action" in prompt
        assert "ROUTING RULE (ABSOLUTE)" in prompt
