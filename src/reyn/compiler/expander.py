from typing import Any
from pydantic import TypeAdapter
from .ir import ArtifactDef, PhaseDef, SkillDef
from reyn.schemas.models import Skill, Phase, SkillGraph, SkillNodeSpec, PreprocessorStep
from reyn.permissions.permissions import PermissionDecl

_PreprocessorAdapter = TypeAdapter(list[PreprocessorStep])


def artifact_to_json_schema(art: ArtifactDef) -> dict[str, Any]:
    """Wrap data schema with {type, data} if wrapped=True, else return as-is."""
    if not art.wrapped:
        return art.schema
    return {
        "type": "object",
        "properties": {
            "type": {"type": "string", "const": art.name},
            "data": art.schema,
        },
        "required": ["type", "data"],
    }


def _union_schema(arts: list[ArtifactDef]) -> dict[str, Any]:
    if len(arts) == 1:
        return artifact_to_json_schema(arts[0])
    return {"anyOf": [artifact_to_json_schema(a) for a in arts]}


def expand_phase(
    phase_def: PhaseDef,
    input_arts: list[ArtifactDef],
) -> Phase:
    input_schema = _union_schema(input_arts) if input_arts else {"type": "object"}
    if len(input_arts) == 1:
        input_schema_name = input_arts[0].name
        input_description = input_arts[0].description
    elif input_arts:
        input_schema_name = " | ".join(a.name for a in input_arts)
        input_description = " | ".join(
            f"{a.name}: {a.description}" if a.description else a.name
            for a in input_arts
        )
    else:
        input_schema_name = "artifact"
        input_description = ""
    try:
        preprocessor = _PreprocessorAdapter.validate_python(phase_def.preprocessor)
    except Exception as exc:
        raise ValueError(
            f"Phase '{phase_def.name}': invalid preprocessor definition — {exc}"
        ) from exc

    # allowed_ops: PhaseDef.allowed_ops is None when frontmatter omitted the
    # key; in that case let the Phase model default factory supply
    # ["file", "ask_user"]. An explicit empty list means "no ops".
    phase_kwargs: dict = dict(
        name=phase_def.name,
        role=phase_def.role,
        input_schema=input_schema,
        input_schema_name=input_schema_name,
        input_description=input_description,
        instructions=phase_def.instructions,
        max_act_turns=phase_def.max_act_turns,
        model_class=phase_def.model_class,
        permissions=PermissionDecl.from_dict(phase_def.permissions),
        preprocessor=preprocessor,
    )
    if phase_def.allowed_ops is not None:
        phase_kwargs["allowed_ops"] = phase_def.allowed_ops
    return Phase(**phase_kwargs)


def expand_skill(
    skill_def: SkillDef,
    phase_defs: dict[str, PhaseDef],
    artifact_defs: dict[str, ArtifactDef],
    phase_objects: dict[str, Phase],
    skill_node_specs: dict[str, SkillNodeSpec] | None = None,
    preprocessor_sub_skills: dict | None = None,
) -> Skill:
    transitions: dict[str, list[str]] = {name: [] for name in phase_objects}
    for node_id in (skill_node_specs or {}):
        transitions.setdefault(node_id, [])
    for src, dst in skill_def.edges:
        transitions.setdefault(src, [])
        if dst not in transitions[src]:
            transitions[src].append(dst)

    used_phases = {skill_def.entry} | {dst for _, dst in skill_def.edges}
    can_finish_phases = [
        name for name, pd in phase_defs.items()
        if pd.can_finish and name in used_phases
    ]

    final_art = artifact_defs.get(skill_def.final_output)
    if final_art:
        final_output_schema = artifact_to_json_schema(final_art)
        final_output_name = final_art.name
    else:
        final_output_schema = {"type": "object"}
        final_output_name = skill_def.final_output

    return Skill(
        name=skill_def.name,
        description=skill_def.description,
        doc=skill_def.doc,
        entry_phase=skill_def.entry,
        phases=phase_objects,
        graph=SkillGraph(
            transitions=transitions,
            can_finish_phases=can_finish_phases,
            skill_nodes=skill_node_specs or {},
        ),
        final_output_schema=final_output_schema,
        final_output_name=final_output_name,
        final_output_description=skill_def.final_output_description,
        finish_criteria=skill_def.finish_criteria,
        preprocessor_sub_skills=preprocessor_sub_skills or {},
    )
