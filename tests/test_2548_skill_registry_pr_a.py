"""Tier 1: Contract — skill registry: skills.yaml parse + cross-tier union-merge + SP rendering (#2548 PR-A).

Covers:
  - ``SkillEntry`` dataclass fields and defaults.
  - ``build_skill_registry`` parses explicit entries from raw config dict.
  - ``build_skill_registry`` filters out ``enabled=False`` entries.
  - Cross-tier ``_merge`` union: later-tier entries win on name collision; entries from
    earlier tiers survive when no collision.
  - ``.reyn/config/skills.yaml`` is read as part of ``load_config`` (dynamic layer).
  - Tier 2 (OS invariant): the built system-prompt CONTAINS the ``## Skills`` heading
    and each skill's name / description / path when N enabled+auto_invoke skills exist.
  - Tier 2 (OS invariant): ``## Skills`` is absent from the SP when the list is empty.
  - Filtering: ``enabled=False`` and ``auto_invoke=False`` skills excluded from SP list.
"""
from __future__ import annotations

import os
from pathlib import Path

from reyn.config.loader import _merge, load_config
from reyn.data.skills.registry import SkillEntry, build_skill_registry
from reyn.tools.schemes._universal_sp import build_universal_tool_use_slots

# ── SkillEntry dataclass ──────────────────────────────────────────────────────


def test_skill_entry_defaults() -> None:
    """Tier 1: SkillEntry defaults: enabled=True, auto_invoke=True."""
    e = SkillEntry(name="foo", description="does foo", path="skills/foo/SKILL.md")
    assert e.enabled is True
    assert e.auto_invoke is True
    assert e.name == "foo"
    assert e.description == "does foo"
    assert e.path == "skills/foo/SKILL.md"


def test_skill_entry_explicit_disabled() -> None:
    """Tier 1: SkillEntry can be created with enabled=False, auto_invoke=False."""
    e = SkillEntry(name="bar", description="does bar", path="skills/bar/SKILL.md",
                   enabled=False, auto_invoke=False)
    assert e.enabled is False
    assert e.auto_invoke is False


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
    assert entry.auto_invoke is True


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
    sp = slots.get("slot_post_catalog", "")
    assert "## Skills" in sp, "## Skills heading missing from slot_post_catalog"
    assert "code-review" in sp
    assert "Review pull requests" in sp
    assert "skills/code-review/SKILL.md" in sp
    assert "deploy" in sp
    assert "Deploy to production" in sp
    assert "skills/deploy/SKILL.md" in sp


def test_sp_skills_block_absent_when_no_skills() -> None:
    """Tier 2: ## Skills section is absent from slot_post_catalog when skills list is empty."""
    slots_empty = build_universal_tool_use_slots(
        universal_wrappers_enabled=True,
        search_actions_enabled=False,
        discovery_mandate=False,
        has_hot_list_aliases=False,
        available_skills=[],
    )
    assert "slot_post_catalog" not in slots_empty, (
        "slot_post_catalog should not be set when available_skills is empty"
    )

    slots_none = build_universal_tool_use_slots(
        universal_wrappers_enabled=True,
        search_actions_enabled=False,
        discovery_mandate=False,
        has_hot_list_aliases=False,
        available_skills=None,
    )
    assert "slot_post_catalog" not in slots_none, (
        "slot_post_catalog should not be set when available_skills is None"
    )


def test_sp_skills_block_excludes_disabled_skills() -> None:
    """Tier 2: skills with enabled=False are not rendered in the SP list."""
    skills = [
        SkillEntry(name="visible", description="should appear", path="skills/vis/SKILL.md",
                   enabled=True, auto_invoke=True),
        SkillEntry(name="hidden", description="should not appear", path="skills/hid/SKILL.md",
                   enabled=False, auto_invoke=True),
    ]
    slots = build_universal_tool_use_slots(
        universal_wrappers_enabled=True,
        search_actions_enabled=False,
        discovery_mandate=False,
        has_hot_list_aliases=False,
        available_skills=skills,
    )
    sp = slots.get("slot_post_catalog", "")
    assert "visible" in sp, "enabled=True skill should appear in SP"
    assert "hidden" not in sp, "enabled=False skill must not appear in SP"


def test_sp_skills_block_excludes_non_auto_invoke_skills() -> None:
    """Tier 2: skills with auto_invoke=False are not rendered in the SP list."""
    skills = [
        SkillEntry(name="auto", description="shown", path="skills/auto/SKILL.md",
                   enabled=True, auto_invoke=True),
        SkillEntry(name="manual", description="hidden from sp", path="skills/manual/SKILL.md",
                   enabled=True, auto_invoke=False),
    ]
    slots = build_universal_tool_use_slots(
        universal_wrappers_enabled=True,
        search_actions_enabled=False,
        discovery_mandate=False,
        has_hot_list_aliases=False,
        available_skills=skills,
    )
    sp = slots.get("slot_post_catalog", "")
    assert "auto" in sp, "auto_invoke=True skill should appear"
    assert "manual" not in sp, "auto_invoke=False skill must not appear in SP"


def test_sp_skills_block_absent_when_all_filtered_out() -> None:
    """Tier 2: slot_post_catalog absent when all skills are filtered out (disabled/no-auto-invoke)."""
    skills = [
        SkillEntry(name="no-show", description="filtered", path="skills/no/SKILL.md",
                   enabled=False, auto_invoke=True),
    ]
    slots = build_universal_tool_use_slots(
        universal_wrappers_enabled=True,
        search_actions_enabled=False,
        discovery_mandate=False,
        has_hot_list_aliases=False,
        available_skills=skills,
    )
    assert "slot_post_catalog" not in slots, (
        "slot_post_catalog should be absent when all skills are filtered out"
    )
