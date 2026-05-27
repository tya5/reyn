"""Tier 2: OS invariant — swe_bench stdlib skill loads and compiles cleanly (FP-0008 PR-A).

Verifies that the swe_bench skill.md and all its phase/artifact files load
through the full DSL compile pipeline without error, and that the top-level
skill properties match the spec declared in skill.md.

No mocks.  Uses real load_dsl_skill on the installed on-disk files.
No private-state assertions.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.compiler.loader import load_dsl_skill
from reyn.schemas.models import Skill

# ---------------------------------------------------------------------------
# Shared fixture path
# ---------------------------------------------------------------------------

_SKILL_MD = (
    Path(__file__).parent.parent
    / "src" / "reyn" / "stdlib" / "skills" / "swe_bench" / "skill.md"
)
_SKILL_ROOT = _SKILL_MD.parent.parent.parent  # src/reyn/stdlib/


def _load() -> Skill:
    """Load the swe_bench skill via the full compile pipeline."""
    return load_dsl_skill(_SKILL_MD, skill_root=_SKILL_ROOT)


# ---------------------------------------------------------------------------
# Test 1: skill.md exists on disk
# ---------------------------------------------------------------------------


def test_swe_bench_skill_md_exists():
    """Tier 2: swe_bench/skill.md exists at the expected stdlib path."""
    assert _SKILL_MD.exists(), f"skill.md not found at {_SKILL_MD}"


# ---------------------------------------------------------------------------
# Test 2: full compile pipeline succeeds
# ---------------------------------------------------------------------------


def test_swe_bench_skill_compiles():
    """Tier 2: load_dsl_skill compiles swe_bench/skill.md without raising.

    Guards that all phase and artifact files are present, parseable, and
    consistent with the skill frontmatter.  A load error would indicate a
    missing artifact YAML, a malformed phase frontmatter, or an undeclared
    artifact name referenced in a phase's `input:` line.
    """
    skill = _load()
    assert isinstance(skill, Skill)


# ---------------------------------------------------------------------------
# Test 3: skill name matches frontmatter
# ---------------------------------------------------------------------------


def test_swe_bench_skill_name():
    """Tier 2: skill.name == 'swe_bench' after full compile."""
    skill = _load()
    assert skill.name == "swe_bench"


# ---------------------------------------------------------------------------
# Test 4: entry phase is 'setup'
# ---------------------------------------------------------------------------


def test_swe_bench_entry_phase():
    """Tier 2: skill.entry_phase == 'setup' as declared in frontmatter."""
    skill = _load()
    assert skill.entry_phase == "setup"


# ---------------------------------------------------------------------------
# Test 5: final_output_name is 'swe_bench_result'
# ---------------------------------------------------------------------------


def test_swe_bench_final_output_name():
    """Tier 2: skill.final_output_name == 'swe_bench_result'.

    Pins that the final_output frontmatter key resolves to the correct
    artifact YAML, which the OS uses to validate the finish artifact.
    """
    skill = _load()
    assert skill.final_output_name == "swe_bench_result"


# ---------------------------------------------------------------------------
# Test 6: final_output_schema is non-empty
# ---------------------------------------------------------------------------


def test_swe_bench_final_output_schema_nonempty():
    """Tier 2: skill.final_output_schema is a non-empty dict.

    Confirms the swe_bench_result.yaml artifact was parsed and its schema
    was expanded into the Skill object.
    """
    skill = _load()
    assert isinstance(skill.final_output_schema, dict)
    assert skill.final_output_schema, "final_output_schema must not be empty"


# ---------------------------------------------------------------------------
# Test 7: all 6 phases are present
# ---------------------------------------------------------------------------


def test_swe_bench_all_phases_present():
    """Tier 2: skill.phases contains all 6 expected phase names.

    Each phase file (setup, explore, plan, apply, verify, report) must
    parse and compile.  A missing or unreadable phase file raises during
    load_dsl_skill, so this test is a corollary of test_swe_bench_skill_compiles.
    Here we also assert the exact set for explicitness.
    """
    skill = _load()
    expected = {"setup", "explore", "plan", "apply", "verify", "report"}
    actual = set(skill.phases.keys())
    assert expected == actual, (
        f"Phase mismatch. Expected: {sorted(expected)}. Got: {sorted(actual)}"
    )


# ---------------------------------------------------------------------------
# Test 8: graph transitions match the spec
# ---------------------------------------------------------------------------


def test_swe_bench_graph_transitions():
    """Tier 2: skill graph transitions match the spec in skill.md frontmatter.

    Pins each edge in the 6-phase graph so that a typo in the graph YAML
    or a future accidental edit is caught immediately.
    """
    skill = _load()
    t = skill.graph.transitions
    assert t.get("setup") == ["explore"], f"setup transitions: {t.get('setup')}"
    assert t.get("explore") == ["plan"], f"explore transitions: {t.get('explore')}"
    assert t.get("plan") == ["apply"], f"plan transitions: {t.get('plan')}"
    assert sorted(t.get("apply", [])) == ["plan", "verify"], (
        f"apply transitions: {t.get('apply')}"
    )
    assert sorted(t.get("verify", [])) == ["apply", "report"], (
        f"verify transitions: {t.get('verify')}"
    )
    assert t.get("report", []) == [], f"report transitions: {t.get('report')}"


# ---------------------------------------------------------------------------
# Test 9: description field is non-empty
# ---------------------------------------------------------------------------


def test_swe_bench_description_nonempty():
    """Tier 2: skill.description is a non-empty string.

    The description is used by the router to match skills to user intents.
    An empty description prevents the router from selecting the skill.
    """
    skill = _load()
    assert skill.description, "swe_bench skill.description must be non-empty"
