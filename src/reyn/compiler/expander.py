from typing import Any
from pydantic import TypeAdapter
from .ir import ArtifactDef, PhaseDef, SkillDef
from reyn.schemas.models import (
    Skill, Phase, SkillGraph, SkillNodeSpec, PreprocessorStep, Postprocessor,
)
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


def _union_phase_permissions(phases: dict[str, Phase]) -> PermissionDecl:
    """Aggregate every phase's PermissionDecl into a single skill-level decl.

    Used by `expand_skill` to populate `Skill.permissions` from the union of
    declared phase permissions during the migration to skill-level permission
    declarations. Once skills declare permissions at the skill frontmatter
    directly, this function takes the explicit declaration as the base and
    layers the phase union on top (caller controls the merge order).

    Merge rules:
      - shell:        any phase True → union True
      - mcp / tool:   set-union of values (de-duplicated, order preserved
                      by first appearance)
      - file_read /
        file_write:  list-union by (path, scope) tuple
      - python:       list-union by (module, function, mode); first-seen
                      timeout wins (consistent with stdlib's typical
                      uniformity within a skill)
      - allowed_mcp:  inherits from any phase's allowed_mcp list (PR37 sidecar);
                      None on every phase → None at skill level
    """
    shell = False
    mcp_seen: set[str] = set()
    mcp: list[str] = []
    tool_seen: set[str] = set()
    tool: list[str] = []
    fr_seen: set[tuple[str, str]] = set()
    file_read: list[dict] = []
    fw_seen: set[tuple[str, str]] = set()
    file_write: list[dict] = []
    py_seen: set[tuple[str, str, str]] = set()
    python: list = []
    allowed_mcp: list[str] | None = None

    for phase in phases.values():
        d = phase.permissions
        if d.shell:
            shell = True
        for s in d.mcp:
            if s not in mcp_seen:
                mcp_seen.add(s)
                mcp.append(s)
        for t in d.tool:
            if t not in tool_seen:
                tool_seen.add(t)
                tool.append(t)
        for entry in d.file_read:
            key = (entry.get("path", ""), entry.get("scope", "just_path"))
            if key not in fr_seen:
                fr_seen.add(key)
                file_read.append(dict(entry))
        for entry in d.file_write:
            key = (entry.get("path", ""), entry.get("scope", "just_path"))
            if key not in fw_seen:
                fw_seen.add(key)
                file_write.append(dict(entry))
        for p in d.python:
            key = (p.module, p.function, p.mode)
            if key not in py_seen:
                py_seen.add(key)
                python.append(p)
        if d.allowed_mcp is not None:
            if allowed_mcp is None:
                allowed_mcp = list(d.allowed_mcp)
            else:
                for s in d.allowed_mcp:
                    if s not in allowed_mcp:
                        allowed_mcp.append(s)

    return PermissionDecl(
        shell=shell,
        mcp=mcp,
        tool=tool,
        file_read=file_read,
        file_write=file_write,
        python=python,
        allowed_mcp=allowed_mcp,
    )


def _expand_postprocessor(
    raw: dict,
    artifact_defs: dict[str, ArtifactDef],
) -> Postprocessor | None:
    """Convert raw frontmatter postprocessor block into a typed Postprocessor.

    Empty dict → returns None (skill has no postprocessor).

    Schema resolution:
      - `output_schema`: dict literal taken as-is, OR string referencing an
        artifact name in `artifact_defs` (= reuses the same artifact registry
        as preprocessor steps and phase inputs).
      - `output_name` / `output_description`: defaults from referenced
        artifact when output_schema was an artifact-name reference.
      - `steps`: typechecked through the same `_PreprocessorAdapter` since
        `ProcessorStep == PreprocessorStep`.
    """
    if not raw:
        return None

    raw_schema = raw.get("output_schema") or raw.get("output")
    if raw_schema is None:
        raise ValueError(
            "Skill postprocessor: missing 'output_schema' (or 'output') field. "
            "Declare the caller-facing artifact schema or reference an "
            "artifact by name."
        )

    output_name = raw.get("output_name", "artifact")
    output_description = raw.get("output_description", "")

    if isinstance(raw_schema, str):
        art = artifact_defs.get(raw_schema)
        if art is None:
            raise ValueError(
                f"Skill postprocessor: output_schema references unknown "
                f"artifact {raw_schema!r}; declare it in the skill's "
                f"artifact registry."
            )
        output_schema = artifact_to_json_schema(art)
        if output_name == "artifact":  # default; override with artifact name
            output_name = art.name
        if not output_description:
            output_description = art.description
    elif isinstance(raw_schema, dict):
        output_schema = raw_schema
    else:
        raise ValueError(
            f"Skill postprocessor: output_schema must be a dict literal or "
            f"a string artifact name, got {type(raw_schema).__name__}"
        )

    steps_raw = raw.get("steps", []) or []
    try:
        steps = _PreprocessorAdapter.validate_python(steps_raw)
    except Exception as exc:
        raise ValueError(
            f"Skill postprocessor: invalid step definition — {exc}"
        ) from exc

    return Postprocessor(
        output_schema=output_schema,
        output_name=output_name,
        output_description=output_description,
        steps=steps,
    )


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
        permissions=_union_phase_permissions(phase_objects),
        postprocessor=_expand_postprocessor(skill_def.postprocessor, artifact_defs),
        preprocessor_sub_skills=preprocessor_sub_skills or {},
    )
