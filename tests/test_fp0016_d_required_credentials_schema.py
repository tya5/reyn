"""Tier 1: Contract tests — FP-0016 Component D: required_credentials field.

Covers the full parse → IR → expand → Skill pipeline for the
`required_credentials` frontmatter field that declares per-skill credential
scoping (FP-0016).

Contract invariants:
1. Skill constructed without required_credentials defaults to ["*"]
2. Skill constructed with required_credentials=["github"] preserves the list
3. Skill constructed with required_credentials=[] preserves the empty list
4. Parser reads required_credentials: [foo, bar] from frontmatter into SkillDef
5. Parser: omitted field → SkillDef.required_credentials is None
6. Expander default-fills: SkillDef.required_credentials=None → Skill.required_credentials=["*"]
7. Parser rejects non-list value (bare string) with a clear error message

No mocks; uses real parse_skill, expand_skill, and Skill/SkillDef constructors.
"""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from reyn.core.compiler.expander import expand_phase, expand_skill
from reyn.core.compiler.ir import ArtifactDef, PhaseDef, SkillDef
from reyn.core.compiler.parser import parse_skill
from reyn.schemas.models import Skill

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _basic_skill_def(*, required_credentials=None, rc_sentinel=False) -> SkillDef:
    """Minimal SkillDef for expander tests.

    rc_sentinel=True → leave required_credentials at its dataclass default (None).
    Otherwise pass required_credentials explicitly.
    """
    kwargs: dict = dict(
        name="test_skill",
        description="",
        doc="",
        entry="run",
        edges=[],
        skill_nodes={},
        final_output="result",
        final_output_description="",
        finish_criteria=[],
        postprocessor={},
        permissions={},
        search_hints=[],
    )
    if not rc_sentinel:
        kwargs["required_credentials"] = required_credentials
    return SkillDef(**kwargs)


def _basic_artifacts() -> dict[str, ArtifactDef]:
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
    return PhaseDef(
        name=name,
        inputs=["user_input"],
        role=None,
        can_finish=can_finish,
        instructions="",
    )


def _minimal_skill_md(extra_frontmatter: str = "") -> str:
    """Minimal valid skill.md content for parser tests.

    extra_frontmatter is appended as raw YAML lines (no leading spaces added).
    """
    base = textwrap.dedent(
        """\
        ---
        name: test_skill
        entry: run
        graph:
          run: []
        final_output: result
        """
    )
    if extra_frontmatter:
        base += extra_frontmatter.strip() + "\n"
    base += "---\nA minimal test skill.\n"
    return base


def _expand(skill_def: SkillDef) -> Skill:
    """Expand a SkillDef to a Skill using minimal artifacts/phases."""
    artifact_defs = _basic_artifacts()
    phase_def = _phase_def("run", can_finish=True)
    phase_obj = expand_phase(phase_def, [artifact_defs["user_input"]])
    return expand_skill(
        skill_def,
        phase_defs={"run": phase_def},
        artifact_defs=artifact_defs,
        phase_objects={"run": phase_obj},
    )


# ---------------------------------------------------------------------------
# Test 1: Skill model default
# ---------------------------------------------------------------------------


def test_skill_default_required_credentials() -> None:
    """Tier 1: Skill constructed without required_credentials defaults to ["*"].

    Confirms backward-compatibility: all pre-FP-0016 skills implicitly get
    full credential delegation.
    """
    sd = _basic_skill_def(required_credentials=None)
    skill = _expand(sd)
    assert skill.required_credentials == ["*"]


# ---------------------------------------------------------------------------
# Test 2: Skill model preserves non-empty list
# ---------------------------------------------------------------------------


def test_skill_preserves_scoped_credentials() -> None:
    """Tier 1: Skill constructed with required_credentials=["github"] preserves the list."""
    sd = _basic_skill_def(required_credentials=["github"])
    skill = _expand(sd)
    assert skill.required_credentials == ["github"]


# ---------------------------------------------------------------------------
# Test 3: Skill model preserves empty list
# ---------------------------------------------------------------------------


def test_skill_preserves_empty_credentials_list() -> None:
    """Tier 1: Skill constructed with required_credentials=[] preserves the empty list.

    An explicit empty list means the skill requires no credentials — distinct
    from the ["*"] default (= full delegation).
    """
    sd = _basic_skill_def(required_credentials=[])
    skill = _expand(sd)
    assert skill.required_credentials == []


# ---------------------------------------------------------------------------
# Test 4: Parser reads list from frontmatter
# ---------------------------------------------------------------------------


def test_parser_reads_required_credentials_list(tmp_path: Path) -> None:
    """Tier 1: parse_skill reads required_credentials list from frontmatter into SkillDef."""
    skill_md = tmp_path / "skill.md"
    skill_md.write_text(
        _minimal_skill_md(
            "required_credentials:\n  - foo\n  - bar"
        ),
        encoding="utf-8",
    )
    sd = parse_skill(skill_md)
    assert sd.required_credentials == ["foo", "bar"]


# ---------------------------------------------------------------------------
# Test 5: Parser: omitted field yields None
# ---------------------------------------------------------------------------


def test_parser_omitted_required_credentials_is_none(tmp_path: Path) -> None:
    """Tier 1: skill.md without required_credentials → SkillDef.required_credentials is None.

    None is the sentinel that tells the expander to apply the ["*"] default,
    preserving backward-compat for existing skills.
    """
    skill_md = tmp_path / "skill.md"
    skill_md.write_text(_minimal_skill_md(), encoding="utf-8")
    sd = parse_skill(skill_md)
    assert sd.required_credentials is None


# ---------------------------------------------------------------------------
# Test 6: Expander default-fills None → ["*"]
# ---------------------------------------------------------------------------


def test_expander_fills_none_with_wildcard_default(tmp_path: Path) -> None:
    """Tier 1: SkillDef.required_credentials=None → Skill.required_credentials=["*"].

    Exercises the expander path: when the parser produced None (= field
    omitted), the expander must substitute ["*"] so the Skill model always
    has a defined credential scope.
    """
    skill_md = tmp_path / "skill.md"
    skill_md.write_text(_minimal_skill_md(), encoding="utf-8")
    sd = parse_skill(skill_md)
    assert sd.required_credentials is None  # pre-condition
    skill = _expand(sd)
    assert skill.required_credentials == ["*"]


# ---------------------------------------------------------------------------
# Test 7: Parser rejects non-list value
# ---------------------------------------------------------------------------


def test_parser_rejects_bare_string_required_credentials(tmp_path: Path) -> None:
    """Tier 1: required_credentials: github (bare string) → ValueError with clear message.

    Bare strings are a common YAML authoring mistake. The parser must reject
    them with an error message that names the field and explains the expected
    format, so skill authors can self-diagnose quickly.
    """
    skill_md = tmp_path / "skill.md"
    skill_md.write_text(
        _minimal_skill_md("required_credentials: github_token"),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="required_credentials"):
        parse_skill(skill_md)
