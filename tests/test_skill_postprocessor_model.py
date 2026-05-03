"""Tier 2: Skill.postprocessor field + frontmatter parsing + expander conversion.

These tests pin the contract that:

1. The `postprocessor:` block in skill.md frontmatter parses into
   `SkillDef.postprocessor` (raw dict).
2. `expand_skill` converts the raw dict into a typed `Postprocessor` model.
3. Skills without a postprocessor block default to `Skill.postprocessor = None`
   (= preserves existing behaviour).
4. The step set is identical to preprocessor (same `PreprocessorStep`
   discriminated union), enforced by the same TypeAdapter.

No mocks; constructs real `SkillDef` / `PhaseDef` / `ArtifactDef` objects and
runs the expander.
"""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from reyn.compiler.expander import expand_phase, expand_skill
from reyn.compiler.ir import ArtifactDef, PhaseDef, SkillDef
from reyn.compiler.parser import parse_skill
from reyn.schemas.models import Phase, Postprocessor, Skill


# ── Helpers ───────────────────────────────────────────────────────────────────


def _basic_artifacts() -> dict[str, ArtifactDef]:
    return {
        "input_art": ArtifactDef(
            name="input_art",
            schema={"type": "object", "properties": {"x": {"type": "string"}}},
            description="Input",
            wrapped=True,
        ),
        "out_art": ArtifactDef(
            name="out_art",
            schema={"type": "object", "properties": {"y": {"type": "string"}}},
            description="LLM output",
            wrapped=True,
        ),
        "post_art": ArtifactDef(
            name="post_art",
            schema={
                "type": "object",
                "properties": {"y": {"type": "string"}, "y_normalized": {"type": "string"}},
            },
            description="Postprocessor output",
            wrapped=True,
        ),
    }


def _phase_def(name: str, *, can_finish: bool = False) -> PhaseDef:
    return PhaseDef(
        name=name,
        inputs=["input_art"],
        role=None,
        can_finish=can_finish,
        instructions="",
    )


def _skill_def(*, postprocessor: dict | None = None) -> SkillDef:
    return SkillDef(
        name="t",
        description="",
        doc="",
        entry="a",
        edges=[],
        skill_nodes={},
        final_output="out_art",
        final_output_description="",
        finish_criteria=[],
        postprocessor=postprocessor or {},
    )


def _build(*, postprocessor: dict | None = None) -> Skill:
    artifacts = _basic_artifacts()
    pd = _phase_def("a", can_finish=True)
    phase_obj = expand_phase(pd, [artifacts["input_art"]])
    sd = _skill_def(postprocessor=postprocessor)
    return expand_skill(sd, {"a": pd}, artifacts, {"a": phase_obj})


# ── Tier 2: default = None ───────────────────────────────────────────────────


def test_skill_postprocessor_default_is_none() -> None:
    """Tier 2: skill without `postprocessor:` block has Skill.postprocessor = None."""
    skill = _build(postprocessor=None)
    assert skill.postprocessor is None


def test_skill_postprocessor_empty_dict_is_none() -> None:
    """Tier 2: empty `postprocessor: {}` block also yields None (no postprocessor)."""
    skill = _build(postprocessor={})
    assert skill.postprocessor is None


# ── Tier 2: dict literal output_schema ───────────────────────────────────────


def test_skill_postprocessor_dict_literal_output_schema() -> None:
    """Tier 2: output_schema can be a dict literal, used verbatim."""
    schema = {"type": "object", "properties": {"caller": {"type": "string"}}}
    skill = _build(postprocessor={
        "output_schema": schema,
        "steps": [],
    })
    assert isinstance(skill.postprocessor, Postprocessor)
    assert skill.postprocessor.output_schema == schema
    assert skill.postprocessor.output_name == "artifact"
    assert skill.postprocessor.steps == []


# ── Tier 2: artifact-name reference for output_schema ────────────────────────


def test_skill_postprocessor_artifact_name_reference() -> None:
    """Tier 2: output_schema as a string references an artifact in the registry."""
    skill = _build(postprocessor={
        "output_schema": "post_art",
        "steps": [],
    })
    assert isinstance(skill.postprocessor, Postprocessor)
    # Wrapped JSON Schema (= {type, data} shape) since artifact has wrapped=True.
    assert skill.postprocessor.output_schema["type"] == "object"
    assert "data" in skill.postprocessor.output_schema["properties"]
    # Artifact-driven defaults populate name + description.
    assert skill.postprocessor.output_name == "post_art"
    assert skill.postprocessor.output_description == "Postprocessor output"


def test_skill_postprocessor_unknown_artifact_name_raises() -> None:
    """Tier 2: referencing an unknown artifact name fails fast at expand."""
    with pytest.raises(ValueError, match=r"unknown artifact"):
        _build(postprocessor={
            "output_schema": "no_such_artifact",
            "steps": [],
        })


# ── Tier 2: missing output_schema is a hard error ────────────────────────────


def test_skill_postprocessor_missing_output_schema_raises() -> None:
    """Tier 2: declaring `postprocessor:` without `output_schema` raises ValueError."""
    with pytest.raises(ValueError, match=r"missing 'output_schema'"):
        _build(postprocessor={
            "steps": [],
        })


# ── Tier 2: step set is the preprocessor step set ────────────────────────────


def test_skill_postprocessor_steps_typecheck_preprocessor_steps() -> None:
    """Tier 2: postprocessor steps reuse the PreprocessorStep discriminated union."""
    skill = _build(postprocessor={
        "output_schema": {"type": "object"},
        "steps": [
            {"type": "validate", "schema": {"type": "object"}},
        ],
    })
    assert isinstance(skill.postprocessor, Postprocessor)
    assert len(skill.postprocessor.steps) == 1
    assert skill.postprocessor.steps[0].type == "validate"


def test_skill_postprocessor_invalid_step_type_raises() -> None:
    """Tier 2: invalid step type is rejected during expansion."""
    with pytest.raises(ValueError, match=r"invalid step definition"):
        _build(postprocessor={
            "output_schema": {"type": "object"},
            "steps": [
                {"type": "no_such_step"},
            ],
        })


# ── Tier 2: parser reads postprocessor block from skill.md frontmatter ───────


def test_parser_reads_postprocessor_block(tmp_path: Path) -> None:
    """Tier 2: parse_skill reads `postprocessor:` from frontmatter into SkillDef."""
    skill_md = tmp_path / "skill.md"
    skill_md.write_text(
        textwrap.dedent(
            """\
            ---
            name: t
            entry: a
            graph:
              a: []
            final_output: out_art
            postprocessor:
              output_schema:
                type: object
                properties:
                  y: {type: string}
              steps:
                - type: validate
                  schema:
                    type: object
            ---
            doc body
            """
        ),
        encoding="utf-8",
    )
    sd = parse_skill(skill_md)
    assert sd.postprocessor != {}
    assert sd.postprocessor["output_schema"]["type"] == "object"
    assert sd.postprocessor["steps"][0]["type"] == "validate"


def test_parser_postprocessor_must_be_mapping(tmp_path: Path) -> None:
    """Tier 2: scalar / list `postprocessor:` is rejected at parse time."""
    skill_md = tmp_path / "skill.md"
    skill_md.write_text(
        textwrap.dedent(
            """\
            ---
            name: t
            entry: a
            graph:
              a: []
            final_output: out_art
            postprocessor: not_a_mapping
            ---
            body
            """
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match=r"must be a mapping"):
        parse_skill(skill_md)
