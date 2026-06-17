"""Tier 2: OS invariant — search_hints field in Skill schema (FP-0024 Component B).

Pins the contract for Tool2Vec-style retrieval hints:

1. skill.md frontmatter with `search_hints:` populates ``Skill.search_hints``
   through the full compile pipeline (parse → expand → Skill).
2. skill.md without `search_hints:` defaults to ``Skill.search_hints = None``
   (backward compatibility preserved).
3. ``search_hints`` are retained after the full ``load_dsl_skill`` round-trip
   (parse + expand), confirming the field survives expander.

No mocks; uses real ``parse_skill``, ``expand_skill``, ``load_dsl_skill``, and
real on-disk fixture files.

Reference: docs/deep-dives/proposals/0024-router-sp-semantic-tool-selection.md
Component B — skill.md search_hints frontmatter field.
"""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from reyn.core.compiler.expander import expand_phase, expand_skill
from reyn.core.compiler.ir import ArtifactDef, PhaseDef, SkillDef
from reyn.core.compiler.loader import load_dsl_skill
from reyn.core.compiler.parser import parse_skill

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FIXTURE_SKILL_WITH_HINTS = (
    Path(__file__).parent / "fixtures" / "skills" / "skill_with_hints" / "skill.md"
)


def _basic_artifacts() -> dict[str, ArtifactDef]:
    """Minimal artifact registry for in-memory expand tests."""
    return {
        "user_input": ArtifactDef(
            name="user_input",
            schema={"type": "object", "properties": {"query": {"type": "string"}}},
            description="Input",
            wrapped=True,
        ),
        "result": ArtifactDef(
            name="result",
            schema={"type": "object", "properties": {"output": {"type": "string"}}},
            description="Output",
            wrapped=True,
        ),
    }


def _phase_def(name: str, *, can_finish: bool = False) -> PhaseDef:
    return PhaseDef(name=name, inputs=["user_input"], role=None, can_finish=can_finish, instructions="")


# ---------------------------------------------------------------------------
# Test 1: skill.md with search_hints parses correctly
# ---------------------------------------------------------------------------


def test_skill_with_search_hints_parses_correctly(tmp_path: Path) -> None:
    """Tier 2: parse_skill reads `search_hints:` list from frontmatter into SkillDef.

    Confirms the parser populates SkillDef.search_hints with the exact
    strings from the frontmatter list, trimmed of whitespace.
    """
    skill_md = tmp_path / "skill.md"
    skill_md.write_text(
        textwrap.dedent(
            """\
            ---
            name: example_skill
            entry: run
            graph:
              run: []
            final_output: result
            search_hints:
              - "review this code and suggest improvements"
              - "check my pull request for issues"
              - "audit the security of this file"
            ---
            A test skill with search hints.
            """
        ),
        encoding="utf-8",
    )
    sd = parse_skill(skill_md)
    assert sd.search_hints == [
        "review this code and suggest improvements",
        "check my pull request for issues",
        "audit the security of this file",
    ]


# ---------------------------------------------------------------------------
# Test 2: skill.md without search_hints defaults to None (backward compat)
# ---------------------------------------------------------------------------


def test_skill_without_search_hints_defaults_to_none(tmp_path: Path) -> None:
    """Tier 2: skill.md without search_hints field yields Skill.search_hints = None.

    Backward compatibility invariant: existing skill.md files that do not
    declare `search_hints:` must load without error and produce a Skill
    where search_hints is None (not missing, not an empty list — explicitly
    None so callers can distinguish "not provided" from "provided but empty").
    """
    skill_md = tmp_path / "skill.md"
    skill_md.write_text(
        textwrap.dedent(
            """\
            ---
            name: legacy_skill
            entry: run
            graph:
              run: []
            final_output: result
            ---
            A legacy skill without search hints.
            """
        ),
        encoding="utf-8",
    )
    # Parse → SkillDef: empty list (no hints in frontmatter)
    sd = parse_skill(skill_md)
    assert sd.search_hints == []

    # Expand to Skill: expander maps empty list → None (= not provided)
    phase_def = _phase_def("run", can_finish=True)
    artifact_defs = _basic_artifacts()
    phase_obj = expand_phase(phase_def, [artifact_defs["user_input"]])
    skill = expand_skill(
        sd,
        phase_defs={"run": phase_def},
        artifact_defs=artifact_defs,
        phase_objects={"run": phase_obj},
    )
    assert skill.search_hints is None


# ---------------------------------------------------------------------------
# Test 3: search_hints retained through load_dsl_skill (full pipeline)
# ---------------------------------------------------------------------------


def test_search_hints_retained_in_skill_registry() -> None:
    """Tier 2: search_hints survive the full load_dsl_skill compile pipeline.

    Uses the on-disk fixture skill `tests/fixtures/skills/skill_with_hints/`
    which declares three search_hints in its frontmatter.  Confirms that
    parse → artifact/phase resolution → expand → Skill all preserve the
    hints intact.

    This test is the end-to-end guard: if the expander or any intermediate
    step silently drops search_hints, this test catches it.
    """
    skill = load_dsl_skill(_FIXTURE_SKILL_WITH_HINTS)
    assert skill.search_hints == [
        "review this code and suggest improvements",
        "check my pull request for issues",
        "audit the security of this file",
    ]
