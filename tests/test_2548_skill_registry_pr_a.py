"""Tier 1: Contract — skill registry: skills.yaml parse + cross-tier union-merge + SP rendering (#2548 PR-A).

Covers:
  - ``SkillEntry`` dataclass fields and defaults.
  - ``build_skill_registry`` parses explicit entries from raw config dict.
  - ``build_skill_registry`` filters out ``enabled=False`` entries.
  - Cross-tier ``_merge`` union: later-tier entries win on name collision; entries from
    earlier tiers survive when no collision.
  - ``.reyn/config/skills.yaml`` is read as part of ``load_config`` (dynamic layer).
  - Tier 2 (OS invariant): the built system-prompt CONTAINS the ``## Skills`` heading
    and each skill's name / description / path when N enabled skills with
    ``visibility="menu"`` exist.
  - Tier 2 (OS invariant): ``## Skills`` is absent from the SP when the list is empty.
  - Filtering: ``enabled=False`` and non-``menu`` skills excluded from the SP list.

#2971 replaced the ``auto_invoke`` boolean with the three-state ``visibility``
axis; these cases now pin ``visibility="menu"`` where they pinned
``auto_invoke=True``. The ``on_demand`` state (SP-excluded but reachable via
``skill_list``) and the ``auto_invoke``-rejected-at-load contract are pinned in
``tests/test_2971_skill_visibility.py``.
"""
from __future__ import annotations

import os
from pathlib import Path

from reyn.config.loader import _merge, load_config
from reyn.data.skills.registry import SkillEntry, build_skill_registry
from reyn.tools.schemes._universal_sp import build_universal_tool_use_slots

# ── SkillEntry dataclass ──────────────────────────────────────────────────────


def test_skill_entry_defaults() -> None:
    """Tier 1: SkillEntry defaults: enabled=True, visibility="menu"."""
    e = SkillEntry(name="foo", description="does foo", path="skills/foo/SKILL.md")
    assert e.enabled is True
    assert e.visibility == "menu"
    assert e.name == "foo"
    assert e.description == "does foo"
    assert e.path == "skills/foo/SKILL.md"


def test_skill_entry_explicit_disabled() -> None:
    """Tier 1: SkillEntry can be created with enabled=False, visibility="hidden"."""
    e = SkillEntry(name="bar", description="does bar", path="skills/bar/SKILL.md",
                   enabled=False, visibility="hidden")
    assert e.enabled is False
    assert e.visibility == "hidden"


# ── build_skill_registry ──────────────────────────────────────────────────────


def test_build_skill_registry_parses_entries() -> None:
    """Tier 1: build_skill_registry returns SkillEntry list from valid entries dict."""
    raw = {
        "entries": {
            "my-skill": {
                "path": "skills/my-skill/SKILL.md",
                "description": "does something useful",
            }
        }
    }
    entries = build_skill_registry(raw)
    entry_map = {e.name: e for e in entries}
    assert "my-skill" in entry_map, f"expected my-skill in registry entries: {[e.name for e in entries]}"
    entry = entry_map["my-skill"]
    assert entry.name == "my-skill"
    assert entry.description == "does something useful"
    assert entry.path == "skills/my-skill/SKILL.md"
    assert entry.enabled is True
    assert entry.visibility == "menu"


def test_build_skill_registry_filters_disabled() -> None:
    """Tier 1: build_skill_registry excludes entries with enabled=False."""
    raw = {
        "entries": {
            "active": {"path": "skills/active/SKILL.md", "description": "active skill", "enabled": True},
            "inactive": {"path": "skills/inactive/SKILL.md", "description": "hidden", "enabled": False},
        }
    }
    entries = build_skill_registry(raw)
    names = {e.name for e in entries}
    assert "active" in names
    assert "inactive" not in names


def test_build_skill_registry_empty_on_no_entries() -> None:
    """Tier 1: build_skill_registry returns empty list when entries is absent."""
    assert build_skill_registry({}) == []
    assert build_skill_registry({"entries": {}}) == []


def test_build_skill_registry_tolerates_malformed_entry() -> None:
    """Tier 1: build_skill_registry skips non-dict entries without crashing."""
    raw = {
        "entries": {
            "good": {"path": "skills/good/SKILL.md", "description": "ok"},
            "bad": "not-a-dict",
        }
    }
    entries = build_skill_registry(raw)
    names = {e.name for e in entries}
    assert "good" in names
    assert "bad" not in names


def test_build_skill_registry_description_truncated_to_one_line() -> None:
    """Tier 1: multi-line description is truncated to first line only."""
    raw = {
        "entries": {
            "sk": {
                "path": "skills/sk/SKILL.md",
                "description": "first line\nsecond line\nthird line",
            }
        }
    }
    entries = build_skill_registry(raw)
    assert entries[0].description == "first line"


# ── cross-tier _merge union ───────────────────────────────────────────────────


def test_merge_skills_entries_union_no_collision() -> None:
    """Tier 1: _merge unions skills.entries from two tiers when no name collision."""
    base = {
        "skills": {
            "entries": {
                "alpha": {"path": "skills/alpha/SKILL.md", "description": "alpha skill"},
            }
        }
    }
    override = {
        "skills": {
            "entries": {
                "beta": {"path": "skills/beta/SKILL.md", "description": "beta skill"},
            }
        }
    }
    merged = _merge(base, override)
    entries = merged["skills"]["entries"]
    assert "alpha" in entries
    assert "beta" in entries


def test_merge_skills_entries_later_tier_wins_on_collision() -> None:
    """Tier 1: when the same skill name appears in two tiers, the later tier wins."""
    base = {
        "skills": {
            "entries": {
                "shared": {"path": "old/SKILL.md", "description": "old description"},
            }
        }
    }
    override = {
        "skills": {
            "entries": {
                "shared": {"path": "new/SKILL.md", "description": "new description"},
            }
        }
    }
    merged = _merge(base, override)
    entry = merged["skills"]["entries"]["shared"]
    assert entry["path"] == "new/SKILL.md"
    assert entry["description"] == "new description"


def test_merge_skills_base_survives_when_override_has_no_skills() -> None:
    """Tier 1: base skills are preserved when override does not include a skills key."""
    base = {
        "skills": {
            "entries": {
                "alpha": {"path": "skills/alpha/SKILL.md", "description": "alpha"},
            }
        }
    }
    override = {"model": "light"}
    merged = _merge(base, override)
    assert "alpha" in merged["skills"]["entries"]


# ── load_config reads .reyn/config/skills.yaml ───────────────────────────────


def test_load_config_reads_dynamic_skills_yaml(tmp_path: Path) -> None:
    """Tier 1: load_config reads .reyn/config/skills.yaml as the dynamic skills layer."""
    (tmp_path / "reyn.yaml").write_text("model: standard\n", encoding="utf-8")
    skills_cfg_dir = tmp_path / ".reyn" / "config"
    skills_cfg_dir.mkdir(parents=True)
    (skills_cfg_dir / "skills.yaml").write_text(
        "skills:\n  entries:\n    dynamic-skill:\n      path: skills/dyn/SKILL.md\n      description: dynamic\n",
        encoding="utf-8",
    )
    old = os.getcwd()
    os.chdir(tmp_path)
    try:
        cfg = load_config()
    finally:
        os.chdir(old)
    entries = cfg.skills.get("entries", {})
    assert "dynamic-skill" in entries, (
        f"dynamic-skill from .reyn/config/skills.yaml not in cfg.skills.entries: {entries}"
    )


def test_load_config_skills_explicit_wins_over_dynamic_layer(tmp_path: Path) -> None:
    """Tier 1: explicit skills in reyn.yaml survive + later dynamic layer entry wins on collision."""
    (tmp_path / "reyn.yaml").write_text(
        "model: standard\n"
        "skills:\n  entries:\n"
        "    static-skill:\n      path: skills/static/SKILL.md\n      description: from reyn.yaml\n"
        "    shared:\n      path: skills/shared-static/SKILL.md\n      description: static version\n",
        encoding="utf-8",
    )
    skills_cfg_dir = tmp_path / ".reyn" / "config"
    skills_cfg_dir.mkdir(parents=True)
    (skills_cfg_dir / "skills.yaml").write_text(
        "skills:\n  entries:\n"
        "    dynamic-skill:\n      path: skills/dyn/SKILL.md\n      description: dynamic\n"
        "    shared:\n      path: skills/shared-dynamic/SKILL.md\n      description: dynamic version\n",
        encoding="utf-8",
    )
    old = os.getcwd()
    os.chdir(tmp_path)
    try:
        cfg = load_config()
    finally:
        os.chdir(old)
    entries = cfg.skills.get("entries", {})
    # static-skill and dynamic-skill both survive
    assert "static-skill" in entries
    assert "dynamic-skill" in entries
    # dynamic layer wins on the shared collision
    assert entries["shared"]["description"] == "dynamic version", (
        f"Expected dynamic layer to win on collision; got {entries['shared']!r}"
    )


# ── L1 system-prompt ## Skills block ─────────────────────────────────────────


def test_sp_skills_block_present_when_skills_available() -> None:
    """Tier 2: ## Skills heading and each skill's name/description/path present in SP."""
    skills = [
        SkillEntry(name="code-review", description="Review pull requests", path="skills/code-review/SKILL.md"),
        SkillEntry(name="deploy", description="Deploy to production", path="skills/deploy/SKILL.md"),
    ]
    slots = build_universal_tool_use_slots(
        universal_wrappers_enabled=True,
        search_actions_enabled=False,
        discovery_mandate=False,
        has_hot_list_aliases=False,
        available_skills=skills,
    )
    sp = slots.get("slot_post_skills", "")
    assert "## Skills" in sp, "## Skills heading missing from slot_post_skills"
    assert "code-review" in sp
    assert "Review pull requests" in sp
    assert "skills/code-review/SKILL.md" in sp
    assert "deploy" in sp
    assert "Deploy to production" in sp
    assert "skills/deploy/SKILL.md" in sp


def test_sp_skills_block_absent_when_no_skills() -> None:
    """Tier 2: ## Skills section is absent from slot_post_skills when skills list is empty."""
    slots_empty = build_universal_tool_use_slots(
        universal_wrappers_enabled=True,
        search_actions_enabled=False,
        discovery_mandate=False,
        has_hot_list_aliases=False,
        available_skills=[],
    )
    assert "slot_post_skills" not in slots_empty, (
        "slot_post_skills should not be set when available_skills is empty"
    )

    slots_none = build_universal_tool_use_slots(
        universal_wrappers_enabled=True,
        search_actions_enabled=False,
        discovery_mandate=False,
        has_hot_list_aliases=False,
        available_skills=None,
    )
    assert "slot_post_skills" not in slots_none, (
        "slot_post_skills should not be set when available_skills is None"
    )


def test_sp_skills_block_excludes_disabled_skills() -> None:
    """Tier 2: skills with enabled=False are not rendered in the SP list."""
    skills = [
        SkillEntry(name="visible", description="should appear", path="skills/vis/SKILL.md",
                   enabled=True, visibility="menu"),
        SkillEntry(name="hidden", description="should not appear", path="skills/hid/SKILL.md",
                   enabled=False, visibility="menu"),
    ]
    slots = build_universal_tool_use_slots(
        universal_wrappers_enabled=True,
        search_actions_enabled=False,
        discovery_mandate=False,
        has_hot_list_aliases=False,
        available_skills=skills,
    )
    sp = slots.get("slot_post_skills", "")
    assert "visible" in sp, "enabled=True skill should appear in SP"
    assert "hidden" not in sp, "enabled=False skill must not appear in SP"


def test_sp_skills_block_excludes_non_menu_skills() -> None:
    """Tier 2: only visibility="menu" skills are rendered in the SP list.

    Both non-menu states are placed OUTSIDE the boundary here — `on_demand` and
    `hidden` are distinct downstream (skill_list returns the first, never the
    second) but the SP menu must exclude both, so both are pinned.
    """
    skills = [
        SkillEntry(name="auto", description="shown", path="skills/auto/SKILL.md",
                   enabled=True, visibility="menu"),
        SkillEntry(name="manual", description="on demand only", path="skills/manual/SKILL.md",
                   enabled=True, visibility="on_demand"),
        SkillEntry(name="secret", description="never shown", path="skills/secret/SKILL.md",
                   enabled=True, visibility="hidden"),
    ]
    slots = build_universal_tool_use_slots(
        universal_wrappers_enabled=True,
        search_actions_enabled=False,
        discovery_mandate=False,
        has_hot_list_aliases=False,
        available_skills=skills,
    )
    sp = slots.get("slot_post_skills", "")
    assert "auto" in sp, 'visibility="menu" skill should appear'
    assert "manual" not in sp, 'visibility="on_demand" skill must not appear in SP'
    assert "secret" not in sp, 'visibility="hidden" skill must not appear in SP'


def test_sp_skills_block_absent_when_all_filtered_out() -> None:
    """Tier 2: slot_post_skills absent when all skills are filtered out (disabled/non-menu)."""
    skills = [
        SkillEntry(name="no-show", description="filtered", path="skills/no/SKILL.md",
                   enabled=False, visibility="menu"),
    ]
    slots = build_universal_tool_use_slots(
        universal_wrappers_enabled=True,
        search_actions_enabled=False,
        discovery_mandate=False,
        has_hot_list_aliases=False,
        available_skills=skills,
    )
    assert "slot_post_skills" not in slots, (
        "slot_post_skills should be absent when all skills are filtered out"
    )


# ── end-to-end wiring: config → registry → SP through a REAL scheme ───────────
# These exercise the real production functions in sequence (no unit-stubs): the
# config loader, the registry builder, the actual scheme SP builders, and the OS
# system-prompt injector. They are the regression guard for the "pieces built but
# never wired" gap (peer review of PR #2550).


def _sp_base_kwargs() -> dict:
    return {
        "agent_name": "alpha",
        "agent_role": "test agent",
        "available_agents": [],
        "memory_index": {"status": "not_found", "content": ""},
        "file_permissions": None,
        "mcp_servers": None,
        "output_language": None,
        "project_context": "",
    }


def test_e2e_config_to_system_prompt_universal_scheme(tmp_path: Path) -> None:
    """Tier 2: skills.yaml → load_config → registry → universal scheme SP renders ## Skills.

    Full production path: a skill declared in reyn.yaml flows through load_config,
    build_skill_registry, build_universal_tool_use_slots (the universal-category
    scheme's builder), and build_system_prompt — the rendered SP must contain the
    skill's name, description, and path.
    """
    from reyn.runtime.router_system_prompt import build_system_prompt

    (tmp_path / "reyn.yaml").write_text(
        "model: standard\n"
        "skills:\n  entries:\n"
        "    pdf-filler:\n      path: skills/pdf-filler/SKILL.md\n"
        "      description: Fill PDF forms from structured data\n",
        encoding="utf-8",
    )
    old = os.getcwd()
    os.chdir(tmp_path)
    try:
        cfg = load_config()
    finally:
        os.chdir(old)

    skills = build_skill_registry(cfg.skills)
    # Simulate the router layer_ctx the scheme reads (available_skills is the seam).
    slots = build_universal_tool_use_slots(
        universal_wrappers_enabled=True,
        search_actions_enabled=False,
        discovery_mandate=False,
        has_hot_list_aliases=False,
        available_skills=skills,
    )
    prompt = build_system_prompt(tool_use_sp=slots, **_sp_base_kwargs())
    assert "## Skills" in prompt, "## Skills section missing from rendered system prompt"
    assert "pdf-filler" in prompt
    assert "Fill PDF forms from structured data" in prompt
    assert "skills/pdf-filler/SKILL.md" in prompt


def test_e2e_retrieval_scheme_does_not_clobber_skills_block(tmp_path: Path) -> None:
    """Tier 2: under the RETRIEVAL scheme, the ## Skills block survives alongside search SP.

    Regression guard for the clobber bug: retrieval overwrites slot_post_catalog
    with its search-guidance block AFTER build_universal runs. The skills block
    lives in the DEDICATED slot_post_skills, so both must appear in the final SP.
    """
    from reyn.runtime.router_system_prompt import build_system_prompt
    from reyn.tools.schemes.retrieval import RetrievalScheme

    (tmp_path / "reyn.yaml").write_text(
        "model: standard\n"
        "skills:\n  entries:\n"
        "    data-cleaner:\n      path: skills/data-cleaner/SKILL.md\n"
        "      description: Normalize and dedupe tabular data\n",
        encoding="utf-8",
    )
    old = os.getcwd()
    os.chdir(tmp_path)
    try:
        cfg = load_config()
    finally:
        os.chdir(old)

    skills = build_skill_registry(cfg.skills)
    scheme = RetrievalScheme()
    layer_ctx = {
        "search_visible": True,
        "router_model": "standard",
        "non_interactive": False,
        "available_skills": skills,
    }
    # Call the REAL retrieval slot builder (the one that overwrites slot_post_catalog).
    slots = scheme._slots_for(available={}, layer_ctx=layer_ctx, terminal=True)
    prompt = build_system_prompt(tool_use_sp=slots, **_sp_base_kwargs())

    # Both the retrieval search-guidance (slot_post_catalog) AND the skills block
    # (slot_post_skills) must be present — the clobber must not have happened.
    assert "## Skills" in prompt, (
        "## Skills clobbered by retrieval's slot_post_catalog overwrite"
    )
    assert "data-cleaner" in prompt
    assert "Normalize and dedupe tabular data" in prompt
    assert "skills/data-cleaner/SKILL.md" in prompt


def test_e2e_enumerate_scheme_threads_skills(tmp_path: Path) -> None:
    """Tier 2: the enumerate-all scheme also threads available_skills into the SP."""
    from reyn.runtime.router_system_prompt import build_system_prompt

    (tmp_path / "reyn.yaml").write_text(
        "model: standard\n"
        "skills:\n  entries:\n"
        "    linter:\n      path: skills/linter/SKILL.md\n"
        "      description: Run project linters and summarize findings\n",
        encoding="utf-8",
    )
    old = os.getcwd()
    os.chdir(tmp_path)
    try:
        cfg = load_config()
    finally:
        os.chdir(old)

    skills = build_skill_registry(cfg.skills)
    # enumerate-all builder signature (universal_wrappers_enabled=False).
    slots = build_universal_tool_use_slots(
        universal_wrappers_enabled=False,
        search_actions_enabled=False,
        discovery_mandate=False,
        has_hot_list_aliases=False,
        available_skills=skills,
    )
    prompt = build_system_prompt(tool_use_sp=slots, **_sp_base_kwargs())
    assert "## Skills" in prompt
    assert "linter" in prompt
    assert "skills/linter/SKILL.md" in prompt


def test_e2e_no_skills_config_omits_section(tmp_path: Path) -> None:
    """Tier 2: a config with no skills → registry empty → SP has no ## Skills section."""
    from reyn.runtime.router_system_prompt import build_system_prompt

    (tmp_path / "reyn.yaml").write_text("model: standard\n", encoding="utf-8")
    old = os.getcwd()
    os.chdir(tmp_path)
    try:
        cfg = load_config()
    finally:
        os.chdir(old)

    skills = build_skill_registry(cfg.skills)
    slots = build_universal_tool_use_slots(
        universal_wrappers_enabled=True,
        search_actions_enabled=False,
        discovery_mandate=False,
        has_hot_list_aliases=False,
        available_skills=skills,
    )
    prompt = build_system_prompt(tool_use_sp=slots, **_sp_base_kwargs())
    assert "## Skills" not in prompt
