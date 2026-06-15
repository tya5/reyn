"""Tier 2: FP-0034 PR-3b-v `## Action categories` SP section gating.

#1627 Stage 4: ``build_system_prompt`` is now a pure slot-injector. The
``universal_wrappers_enabled`` parameter has been REMOVED from
``build_system_prompt``. Tests now call ``build_universal_tool_use_slots``
directly and pass the result as ``tool_use_sp``.

Coverage:
  - No tool_use_sp (None): SP excludes the section (bare OS frame)
  - universal_wrappers_enabled=False via slot-map: SP excludes the section
  - universal_wrappers_enabled=True via slot-map: SP includes section + all 13 cats
  - Section placement relative to other sections
  - Section content notes qualified-name format + dispatch path

No mocks. Pure-string contract tests on the build_system_prompt output.
"""

from __future__ import annotations

import re

from reyn.chat.router_system_prompt import build_system_prompt
from reyn.tools.schemes._universal_sp import build_universal_tool_use_slots
from reyn.tools.universal_catalog import CATEGORIES

_BASE_KWARGS = {
    "agent_name": "alpha",
    "agent_role": "Test agent",
    "available_skills": [
        {"name": "code_review", "description": "Review code", "category": "analytics"},
    ],
    "available_agents": [],
    "memory_index": {"status": "not_found", "content": ""},
    "file_permissions": None,
    "mcp_servers": None,
    "output_language": None,
    "project_context": "",
    "indexed_sources_section": None,
}


def _slots(*, universal_wrappers_enabled: bool) -> "dict[str, str]":
    return build_universal_tool_use_slots(
        universal_wrappers_enabled=universal_wrappers_enabled,
        search_actions_enabled=True,
        discovery_mandate=False,
        has_hot_list_aliases=False,
        non_interactive=False,
    )


# ── 1. No tool_use_sp (None) excludes the section ────────────────────────


def test_default_flag_off_excludes_action_categories_section() -> None:
    """Tier 2: build_system_prompt() with tool_use_sp=None (bare OS frame) omits
    ## Action categories.

    #1627 Stage 4: None ⇒ {} (empty slot-map). No slot_post_environment injected.
    """
    prompt = build_system_prompt(**_BASE_KWARGS)
    assert "## Action categories" not in prompt


def test_default_matches_explicit_false() -> None:
    """Tier 2: omitting tool_use_sp matches explicit wrappers=False slot-map
    for the Action categories section (both omit it)."""
    default = build_system_prompt(**_BASE_KWARGS)
    explicit = build_system_prompt(**_BASE_KWARGS, tool_use_sp=_slots(universal_wrappers_enabled=False))
    # Both omit the section; bare-frame content differs from slot-map content
    # (slot-map adds Capabilities/Behaviour slots too), so we only pin the section.
    assert "## Action categories" not in default
    assert "## Action categories" not in explicit


# ── 2. Flag on adds the section with all 13 categories ───────────────────


def test_flag_on_adds_action_categories_section() -> None:
    """Tier 2: universal_wrappers_enabled=True in slot-map adds ## Action categories."""
    prompt = build_system_prompt(
        **_BASE_KWARGS, tool_use_sp=_slots(universal_wrappers_enabled=True),
    )
    assert "## Action categories" in prompt


def test_flag_on_lists_all_categories() -> None:
    """Tier 2: section names every category from FP-0034 §D18 master table.

    Issue #879 collapsed mcp.server / mcp.tool / mcp.operation into a
    single ``mcp`` category; the SP bullet count drops accordingly.
    """
    prompt = build_system_prompt(
        **_BASE_KWARGS, tool_use_sp=_slots(universal_wrappers_enabled=True),
    )
    for cat in (
        "skill",
        "multi_agent",
        "mcp",
        "file", "web",
        "memory_entry", "memory_operation",
        "reyn_source",
        "rag_corpus", "rag_operation",
        "exec",
    ):
        assert f"**{cat}**" in prompt, (
            f"category {cat!r} must be listed as a bullet in the SP"
        )


def test_sp_bullets_match_categories_tuple_exactly() -> None:
    """Tier 2: drift-detection — SP `**X**` bullets ≡ ``CATEGORIES`` tuple."""
    prompt = build_system_prompt(
        **_BASE_KWARGS, tool_use_sp=_slots(universal_wrappers_enabled=True),
    )
    section_start = prompt.index("## Action categories")
    section_end = prompt.index("## Behaviour", section_start)
    section = prompt[section_start:section_end]

    bullet_re = re.compile(r"^- \*\*([a-z._]+)\*\*", re.MULTILINE)
    sp_bullets = tuple(bullet_re.findall(section))

    assert sp_bullets == tuple(CATEGORIES), (
        f"SP bullets diverged from CATEGORIES.\n"
        f"  SP:        {sp_bullets}\n"
        f"  CATEGORIES:{tuple(CATEGORIES)}\n"
        f"Update both src/reyn/tools/schemes/_universal_sp.py AND "
        f"src/reyn/tools/universal_catalog.py together."
    )


def test_flag_on_describes_qualified_name_format() -> None:
    """Tier 2: section explains <category>__<entry> qualified-name format."""
    prompt = build_system_prompt(
        **_BASE_KWARGS, tool_use_sp=_slots(universal_wrappers_enabled=True),
    )
    assert "<category>__<entry>" in prompt


def test_flag_on_mentions_three_wrappers() -> None:
    """Tier 2: section names the 3 universal wrappers + hand-off chain."""
    prompt = build_system_prompt(
        **_BASE_KWARGS, tool_use_sp=_slots(universal_wrappers_enabled=True),
    )
    for name in ("list_actions", "describe_action", "invoke_action"):
        assert name in prompt


def test_flag_on_mentions_similar_name_suggestions() -> None:
    """Tier 2: section notes §D12 error-with-suggestions recovery path."""
    prompt = build_system_prompt(
        **_BASE_KWARGS, tool_use_sp=_slots(universal_wrappers_enabled=True),
    )
    assert "list_actions" in prompt


# ── 3. Section placement ──────────────────────────────────────────────────


def test_section_placed_between_capabilities_and_behaviour() -> None:
    """Tier 2: ## Action categories sits after Capabilities, before Behaviour."""
    prompt = build_system_prompt(
        **_BASE_KWARGS, tool_use_sp=_slots(universal_wrappers_enabled=True),
    )
    cap_idx = prompt.index("## Capabilities (routing guide)")
    cats_idx = prompt.index("## Action categories")
    beh_idx = prompt.index("## Behaviour")
    assert cap_idx < cats_idx < beh_idx, (
        f"section order broken: Capabilities@{cap_idx}, "
        f"Categories@{cats_idx}, Behaviour@{beh_idx}"
    )


# ── 4. Flag on does not break the other sections ─────────────────────────


def test_flag_on_preserves_behaviour_and_capabilities_sections() -> None:
    """Tier 2: ## Capabilities and ## Behaviour sections remain when flag on."""
    prompt = build_system_prompt(
        **_BASE_KWARGS, tool_use_sp=_slots(universal_wrappers_enabled=True),
    )
    assert "## Skills" not in prompt
    assert "## Capabilities (routing guide)" in prompt
    assert "## Behaviour" in prompt


def test_flag_on_preserves_behaviour_section() -> None:
    """Tier 2: existing ## Behaviour section still present when flag on."""
    prompt = build_system_prompt(
        **_BASE_KWARGS, tool_use_sp=_slots(universal_wrappers_enabled=True),
    )
    assert "## Behaviour" in prompt
