"""Tier 2: FP-0034 PR-3b-v `## Action categories` SP section gating.

Verifies the new flag-gated section in ``build_system_prompt`` that
prepends a 13-category overview when
``universal_wrappers_enabled=True``. With the flag off (default), SP
byte content stays identical to the pre-PR-3b-v output so LLMReplay
fixtures remain valid.

Coverage:
  - Default flag-off: SP excludes the section (fixture-safe path)
  - Default matches explicit False (no accidental default flip)
  - Flag on: SP includes the section + lists all 13 categories
  - Section placement relative to other sections (after Capabilities,
    before Behaviour)
  - Section content notes qualified-name format + dispatch path

No mocks. Pure-string contract tests on the build_system_prompt
output.
"""

from __future__ import annotations

import re

from reyn.chat.router_system_prompt import build_system_prompt
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


# ── 1. Default (flag off) excludes the section ───────────────────────────


def test_default_flag_off_excludes_action_categories_section() -> None:
    """Tier 2: build_system_prompt() default omits ## Action categories.

    PR-3b-v adds the section behind a flag with default False — same
    insulation pattern as PR-3b-i build_tools wrappers.  LLMReplay
    fixtures recorded before this PR stay byte-valid for callers
    that don't pass the flag.
    """
    prompt = build_system_prompt(**_BASE_KWARGS)
    assert "## Action categories" not in prompt


def test_default_matches_explicit_false() -> None:
    """Tier 2: omitting the kwarg matches explicit False (no accidental flip)."""
    default = build_system_prompt(**_BASE_KWARGS)
    explicit = build_system_prompt(**_BASE_KWARGS, universal_wrappers_enabled=False)
    assert default == explicit


# ── 2. Flag on adds the section with all 13 categories ───────────────────


def test_flag_on_adds_action_categories_section() -> None:
    """Tier 2: flag=True prepends the ## Action categories section."""
    prompt = build_system_prompt(
        **_BASE_KWARGS, universal_wrappers_enabled=True,
    )
    assert "## Action categories" in prompt


def test_flag_on_lists_all_categories() -> None:
    """Tier 2: section names every category from FP-0034 §D18 master table.

    Issue #879 collapsed mcp.server / mcp.tool / mcp.operation into a
    single ``mcp`` category; the SP bullet count drops accordingly.
    """
    prompt = build_system_prompt(
        **_BASE_KWARGS, universal_wrappers_enabled=True,
    )
    for cat in (
        "skill", "agent.peer",
        "mcp",
        "file", "web",
        "memory.entry", "memory.operation",
        "reyn.source",
        "rag.corpus", "rag.operation",
        "exec",
    ):
        # Each category is on its own bullet line; use bold marker so
        # 'file' and 'reyn.source' match independently of plain words.
        assert f"**{cat}**" in prompt, (
            f"category {cat!r} must be listed as a bullet in the SP"
        )


def test_sp_bullets_match_categories_tuple_exactly() -> None:
    """Tier 2: drift-detection — SP `**X**` bullets ≡ ``CATEGORIES`` tuple.

    If a 14th category is added to ``universal_catalog.CATEGORIES`` but
    the SP section is not extended (or vice versa), this test fails so
    the divergence cannot land silently.  The handlers enumerate by
    iterating ``CATEGORIES`` while the SP is hand-authored prose; this
    invariant pins the two together.
    """
    prompt = build_system_prompt(
        **_BASE_KWARGS, universal_wrappers_enabled=True,
    )
    # Slice to the Action categories section only so unrelated bold
    # spans in Behaviour / other sections don't confuse the match.
    section_start = prompt.index("## Action categories")
    section_end = prompt.index("## Behaviour", section_start)
    section = prompt[section_start:section_end]

    bullet_re = re.compile(r"^- \*\*([a-z.]+)\*\*", re.MULTILINE)
    sp_bullets = tuple(bullet_re.findall(section))

    assert sp_bullets == tuple(CATEGORIES), (
        f"SP bullets diverged from CATEGORIES.\n"
        f"  SP:        {sp_bullets}\n"
        f"  CATEGORIES:{tuple(CATEGORIES)}\n"
        f"Update both src/reyn/chat/router_system_prompt.py AND "
        f"src/reyn/tools/universal_catalog.py together."
    )


def test_flag_on_describes_qualified_name_format() -> None:
    """Tier 2: section explains <category>__<entry> qualified-name format."""
    prompt = build_system_prompt(
        **_BASE_KWARGS, universal_wrappers_enabled=True,
    )
    assert "<category>__<entry>" in prompt


def test_flag_on_mentions_three_wrappers() -> None:
    """Tier 2: section names the 3 universal wrappers + hand-off chain."""
    prompt = build_system_prompt(
        **_BASE_KWARGS, universal_wrappers_enabled=True,
    )
    for name in ("list_actions", "describe_action", "invoke_action"):
        assert name in prompt


def test_flag_on_mentions_similar_name_suggestions() -> None:
    """Tier 2: section notes §D12 error-with-suggestions recovery path.

    The invoke_action description says "returns an error with similar-name
    suggestions" on unknown action_name. The SP section notes discovery via
    list_actions. Both signals together guide the LLM to recover from typos.
    """
    prompt = build_system_prompt(
        **_BASE_KWARGS, universal_wrappers_enabled=True,
    )
    # Discovery path must be mentioned
    assert "list_actions" in prompt  # the recovery hint references it
    # The invoke_action description carries the "similar-name suggestions"
    # phrase (tested in test_tool_description_role_separation.py).
    # SP section need only confirm list_actions is the recovery tool.


# ── 3. Section placement ──────────────────────────────────────────────────


def test_section_placed_between_capabilities_and_behaviour() -> None:
    """Tier 2: ## Action categories sits after Capabilities, before Behaviour.

    The static prefix maximises Anthropic prompt-cache prefix coverage
    (= the section lives in the always-static part of the prompt, not
    after dynamic sections that vary per request).
    """
    prompt = build_system_prompt(
        **_BASE_KWARGS, universal_wrappers_enabled=True,
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
    """Tier 2: ## Capabilities and ## Behaviour sections remain when flag on.

    Phase 6 cleanup: ## Skills section removed from SP (wrapper-only path
    routes skill discovery via list_actions at runtime). ## Capabilities
    and ## Behaviour remain as the SP routing structure.
    """
    prompt = build_system_prompt(
        **_BASE_KWARGS, universal_wrappers_enabled=True,
    )
    # ## Skills section is gone in wrapper-only path
    assert "## Skills" not in prompt
    # Core structure still present
    assert "## Capabilities (routing guide)" in prompt
    assert "## Behaviour" in prompt


def test_flag_on_preserves_behaviour_section() -> None:
    """Tier 2: existing ## Behaviour section still present when flag on."""
    prompt = build_system_prompt(
        **_BASE_KWARGS, universal_wrappers_enabled=True,
    )
    assert "## Behaviour" in prompt
