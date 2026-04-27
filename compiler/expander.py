import warnings
from typing import Any
from .ir import ArtifactDef, FieldDef, PhaseDef, AppDef
from agent_os.models import App, Phase, AppGraph


# Primitive DSL type → JSON Schema
_TYPE_MAP: dict[str, dict[str, Any]] = {
    "string":    {"type": "string"},
    "number":    {"type": "number"},
    "integer":   {"type": "integer"},
    "boolean":   {"type": "boolean"},
    "string[]":  {"type": "array", "items": {"type": "string"}},
    "number[]":  {"type": "array", "items": {"type": "number"}},
    "integer[]": {"type": "array", "items": {"type": "integer"}},
    # Weak types — accepted but produce no sub-schema; use sparingly
    "object":    {"type": "object"},
    "array":     {"type": "array"},
    "any":       {},
}

_WEAK_TYPES = {"object", "array", "any"}


def _field_schema(
    f: FieldDef,
    artifact_defs: dict[str, ArtifactDef] | None = None,
) -> dict[str, Any]:
    """
    Resolve a DSL field to a JSON Schema fragment.

    Resolution order:
      1. Inline JSON Schema (f.schema set) → pass through as-is
      2. Primitive type alias → direct mapping via _TYPE_MAP
      3. Artifact reference → inline-expand the artifact's data schema
      4. Weak fallback ("object") → {"type": "object"} with warning
    """
    if f.schema is not None:
        return f.schema

    if f.type_str in _TYPE_MAP:
        if f.type_str in _WEAK_TYPES:
            warnings.warn(
                f"Field '{f.name}' uses weak type '{f.type_str}'. "
                "Consider referencing a concrete artifact instead.",
                stacklevel=4,
            )
        return _TYPE_MAP[f.type_str]

    if artifact_defs and f.type_str in artifact_defs:
        # Inline-expand: embed the artifact's data schema directly.
        # No {type, data} wrapper — the field is a sub-object, not a root artifact.
        return _data_schema(artifact_defs[f.type_str], artifact_defs)

    warnings.warn(
        f"Field '{f.name}' has unknown type '{f.type_str}', falling back to object.",
        stacklevel=4,
    )
    return {"type": "object"}


def _data_schema(
    art: ArtifactDef,
    artifact_defs: dict[str, ArtifactDef] | None = None,
) -> dict[str, Any]:
    """Return the inner data-object JSON Schema (no {type,data} wrapper)."""
    props = {f.name: _field_schema(f, artifact_defs) for f in art.fields}
    required = [f.name for f in art.fields if not f.optional]
    schema: dict[str, Any] = {"type": "object", "properties": props}
    if required:
        schema["required"] = required
    return schema


def artifact_to_json_schema(
    art: ArtifactDef,
    artifact_defs: dict[str, ArtifactDef] | None = None,
) -> dict[str, Any]:
    """
    Convert an ArtifactDef to a JSON Schema.

    wrapped=True  → {type: const, data: <data_schema>}   (phase input)
    wrapped=False → flat data_schema                       (entry / final output)
    """
    data_schema = _data_schema(art, artifact_defs)
    if not art.wrapped:
        return data_schema
    return {
        "type": "object",
        "properties": {
            "type": {"type": "string", "const": art.name},
            "data": data_schema,
        },
        "required": ["type", "data"],
    }


def _union_schema(
    arts: list[ArtifactDef],
    artifact_defs: dict[str, ArtifactDef] | None = None,
) -> dict[str, Any]:
    if len(arts) == 1:
        return artifact_to_json_schema(arts[0], artifact_defs)
    return {"anyOf": [artifact_to_json_schema(a, artifact_defs) for a in arts]}


def expand_phase(
    phase_def: PhaseDef,
    input_arts: list[ArtifactDef],
    artifact_defs: dict[str, ArtifactDef] | None = None,
) -> Phase:
    input_schema = _union_schema(input_arts, artifact_defs) if input_arts else {"type": "object"}
    return Phase(
        name=phase_def.name,
        role=phase_def.role,
        input_schema=input_schema,
        input_description=phase_def.input_description,
        instructions=phase_def.instructions,
        max_act_turns=phase_def.max_act_turns,
    )


def expand_app(
    app_def: AppDef,
    phase_defs: dict[str, PhaseDef],
    artifact_defs: dict[str, ArtifactDef],
    phase_objects: dict[str, Phase],
) -> App:
    transitions: dict[str, list[str]] = {name: [] for name in phase_objects}
    for src, dst in app_def.edges:
        transitions.setdefault(src, [])
        if dst not in transitions[src]:
            transitions[src].append(dst)

    used_phases = {app_def.entry} | {dst for _, dst in app_def.edges}
    can_finish_phases = [
        name for name, pd in phase_defs.items()
        if pd.can_finish and name in used_phases
    ]

    final_art = artifact_defs.get(app_def.final_output)
    if final_art:
        # Use wrapped schema — final output follows the same {type, data} convention
        # as all other artifacts so LLM output is uniform.
        final_output_schema = artifact_to_json_schema(final_art, artifact_defs)
        final_output_name = final_art.name
    else:
        final_output_schema = {"type": "object"}
        final_output_name = app_def.final_output

    return App(
        name=app_def.name,
        entry_phase=app_def.entry,
        phases=phase_objects,
        graph=AppGraph(
            transitions=transitions,
            can_finish_phases=can_finish_phases,
            max_phase_visits=app_def.max_phase_visits,
        ),
        final_output_schema=final_output_schema,
        final_output_name=final_output_name,
        final_output_description=app_def.final_output_description,
        finish_criteria=app_def.finish_criteria,
    )
