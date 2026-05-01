"""
Compile-time type inference for Phase preprocessor chains.

Given a Phase's input_schema and its preprocessor steps, infers the JSON Schema
the LLM will see after the preprocessor runs (the "enriched" schema).

All LLM interaction stays inside sub-skills; this module is purely deterministic.
"""
from __future__ import annotations
import copy
import jsonschema
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from reyn.models import Skill, PreprocessorStep


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


def _op_output_schema(op_kind: str) -> dict:
    """Return a coarse JSON schema for a ControlIROp result.

    These shapes mirror what op_runtime handlers actually return. Callers
    use this for `RunOpStep` schema inference; the schemas are deliberately
    permissive so the LLM tolerates new fields op handlers may add later.
    """
    if op_kind == "file":
        return {
            "type": "object",
            "description": "file op result (read/write/glob/grep/delete/edit). "
                           "Common fields: kind, op, path, status. read adds 'content'.",
        }
    if op_kind == "run_skill":
        return {
            "type": "object",
            "description": "run_skill op result. Fields include status, skill, "
                           "success, final_output (sub-skill's final_output dict), "
                           "phase_artifacts, events_glob, artifacts_glob, workspace.",
        }
    if op_kind == "web_fetch":
        return {
            "type": "object",
            "description": "web_fetch op result: url, status_code, content_type, content, truncated.",
        }
    if op_kind == "web_search":
        return {
            "type": "object",
            "description": "web_search op result: query, backend, results (list of {title, url, snippet}).",
        }
    if op_kind == "shell":
        return {
            "type": "object",
            "description": "shell op result: status, returncode, stdout, stderr.",
        }
    if op_kind == "lint":
        return {
            "type": "object",
            "description": "lint op result: passed, error_count, warning_count, issues.",
        }
    if op_kind == "mcp":
        return {
            "type": "object",
            "description": "mcp op result: status, server, tool, content, raw.",
        }
    return {"type": "object"}


def _infer_step_output_schema(
    step: "PreprocessorStep",
    sub_skills: dict[str, "Skill"],
    step_label: str,
) -> dict:
    """Return the JSON Schema that a single step produces (for use in iterate.apply)."""
    from reyn.models import IterateStep, ValidateStep, RunOpStep
    if isinstance(step, RunOpStep):
        return _op_output_schema(step.op.kind)
    if isinstance(step, ValidateStep):
        raise PreprocessorTypeError(f"{step_label}: validate cannot be used as iterate.apply")
    if isinstance(step, IterateStep):
        raise PreprocessorTypeError(f"{step_label}: nested iterate is not supported in MVP")
    raise PreprocessorTypeError(f"{step_label}: unknown step type {type(step)}")


def infer_llm_visible_schema(
    input_schema: dict[str, Any],
    steps: list["PreprocessorStep"],
    sub_skills: dict[str, "Skill"],
) -> dict[str, Any]:
    """Compute the JSON Schema the LLM sees after the preprocessor chain runs.

    Returns a deep copy of input_schema enriched with fields added by each step.
    Raises PreprocessorTypeError on incompatible or invalid steps.
    """
    from reyn.models import IterateStep, ValidateStep, LintPlanStep, PythonStep, RunOpStep

    schema = copy.deepcopy(input_schema)

    for i, step in enumerate(steps):
        label = f"preprocessor step[{i}] (type={step.type!r})"

        if isinstance(step, RunOpStep):
            if step.into is not None:
                _require_parent_exists(schema, step.into, label)
                schema = _set_at_path(schema, step.into, _op_output_schema(step.op.kind))
            # else: result is discarded; no schema change

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
            element_schema = _infer_step_output_schema(step.apply, sub_skills, f"{label}.apply")
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

        elif isinstance(step, LintPlanStep):
            # Adds a list of issue strings at step.into. Parent path must exist;
            # the over path is checked at runtime against actual artifact data.
            _require_parent_exists(schema, step.into, label)
            schema = _set_at_path(
                schema, step.into,
                {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Deterministic structural-lint issues found in the plan.",
                },
            )

        elif isinstance(step, PythonStep):
            # Adds the user function's return value at step.into. The author
            # declares the schema explicitly — we don't import their Python at
            # compile time (avoids running un-sandboxed top-level side effects).
            _require_parent_exists(schema, step.into, label)
            try:
                jsonschema.Draft7Validator.check_schema(step.output_schema)
            except jsonschema.SchemaError as exc:
                raise PreprocessorTypeError(
                    f"{label}: output_schema is not a valid JSON Schema — {exc.message}"
                ) from exc
            schema = _set_at_path(schema, step.into, step.output_schema)

        else:
            raise PreprocessorTypeError(f"{label}: unknown step type {type(step)}")

    return schema
