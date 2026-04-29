"""
Compile-time type inference for Phase preprocessor chains.

Given a Phase's input_schema and its preprocessor steps, infers the JSON Schema
the LLM will see after the preprocessor runs (the "enriched" schema).

All LLM interaction stays inside sub-apps; this module is purely deterministic.
"""
from __future__ import annotations
import copy
import jsonschema
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from reyn.models import App, PreprocessorStep


class PreprocessorTypeError(ValueError):
    pass


def _get_at_path(schema: dict, path: str) -> Any:
    """Navigate a JSON Schema following dot-notation, walking through 'properties' at each hop.

    e.g. _get_at_path(schema, "data.items") walks
         schema["properties"]["data"]["properties"]["items"]
    """
    parts = path.split(".")
    cur = schema
    for part in parts:
        props = cur.get("properties")
        if not isinstance(props, dict) or part not in props:
            raise PreprocessorTypeError(
                f"Path '{path}': segment '{part}' not found in schema properties. "
                f"Available: {list((props or {}).keys())}"
            )
        cur = props[part]
    return cur


def _set_at_path(schema: dict, path: str, field_schema: dict) -> dict:
    """Return a deep copy of schema with field_schema written at the given dot-path.

    Creates intermediate objects as needed. Does NOT fail if a parent is missing —
    the compile-time check for missing parents is done before calling this.
    """
    result = copy.deepcopy(schema)
    parts = path.split(".")
    cur = result
    for part in parts[:-1]:
        cur.setdefault("properties", {})
        if part not in cur["properties"]:
            cur["properties"][part] = {"type": "object", "properties": {}}
        cur = cur["properties"][part]
    cur.setdefault("properties", {})[parts[-1]] = field_schema
    return result


def _require_parent_exists(schema: dict, path: str, step_label: str) -> None:
    """Raise PreprocessorTypeError if the parent path of 'into' doesn't exist."""
    parts = path.split(".")
    if len(parts) < 2:
        return  # top-level field — always valid
    parent_path = ".".join(parts[:-1])
    try:
        _get_at_path(schema, parent_path)
    except PreprocessorTypeError:
        raise PreprocessorTypeError(
            f"{step_label}: 'into' parent path '{parent_path}' not found in schema. "
            "Ensure the parent field is declared in the input artifact schema or "
            "produced by an earlier preprocessor step."
        )


def _infer_step_output_schema(
    step: "PreprocessorStep",
    sub_apps: dict[str, "App"],
    step_label: str,
) -> dict:
    """Return the JSON Schema that a single step produces (for use in iterate.apply)."""
    from reyn.models import RunAppStep, IterateStep, ValidateStep
    if isinstance(step, RunAppStep):
        if step.app not in sub_apps:
            raise PreprocessorTypeError(
                f"{step_label}: sub-app '{step.app}' not found. "
                f"Available: {list(sub_apps.keys())}"
            )
        return sub_apps[step.app].final_output_schema
    if isinstance(step, ValidateStep):
        raise PreprocessorTypeError(f"{step_label}: validate cannot be used as iterate.apply")
    if isinstance(step, IterateStep):
        raise PreprocessorTypeError(f"{step_label}: nested iterate is not supported in MVP")
    raise PreprocessorTypeError(f"{step_label}: unknown step type {type(step)}")


def infer_llm_visible_schema(
    input_schema: dict[str, Any],
    steps: list["PreprocessorStep"],
    sub_apps: dict[str, "App"],
) -> dict[str, Any]:
    """Compute the JSON Schema the LLM sees after the preprocessor chain runs.

    Returns a deep copy of input_schema enriched with fields added by each step.
    Raises PreprocessorTypeError on incompatible or invalid steps.
    """
    from reyn.models import RunAppStep, IterateStep, ValidateStep

    schema = copy.deepcopy(input_schema)

    for i, step in enumerate(steps):
        label = f"preprocessor step[{i}] (type={step.type!r})"

        if isinstance(step, RunAppStep):
            if step.into is None:
                raise PreprocessorTypeError(
                    f"{label}: top-level run_app must have 'into' set"
                )
            if step.app not in sub_apps:
                raise PreprocessorTypeError(
                    f"{label}: sub-app '{step.app}' not found. "
                    f"Available: {list(sub_apps.keys())}"
                )
            _require_parent_exists(schema, step.into, label)
            field_schema = sub_apps[step.app].final_output_schema
            schema = _set_at_path(schema, step.into, field_schema)

        elif isinstance(step, IterateStep):
            # Verify the 'over' path points to an array in the current schema
            try:
                arr_schema = _get_at_path(schema, step.over)
            except PreprocessorTypeError as exc:
                raise PreprocessorTypeError(
                    f"{label}: 'over' path error — {exc}"
                ) from exc
            if arr_schema.get("type") != "array":
                raise PreprocessorTypeError(
                    f"{label}: 'over' path '{step.over}' must point to an array schema "
                    f"(got type={arr_schema.get('type')!r})"
                )
            _require_parent_exists(schema, step.into, label)
            element_schema = _infer_step_output_schema(step.apply, sub_apps, f"{label}.apply")
            schema = _set_at_path(schema, step.into, {"type": "array", "items": element_schema})

        elif isinstance(step, ValidateStep):
            # Compile-time: meta-validate — confirm step.schema_ is a valid JSON Schema.
            # This catches authoring errors (unknown keywords, type typos, etc.).
            # Semantic compatibility against enriched_schema is intentionally NOT checked:
            # validate's purpose is to add stricter constraints that may not be derivable
            # from the upstream schema (e.g. minimum: 0.01 where only type: number is known).
            # Runtime will validate artifact["data"] against step.schema_.
            try:
                jsonschema.Draft7Validator.check_schema(step.schema_)
            except jsonschema.SchemaError as exc:
                raise PreprocessorTypeError(
                    f"{label}: validate.schema is not a valid JSON Schema — {exc.message}"
                ) from exc
            # Inferred schema unchanged (pass-through; runtime enforces the assertion).

        else:
            raise PreprocessorTypeError(f"{label}: unknown step type {type(step)}")

    return schema
