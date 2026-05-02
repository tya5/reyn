"""
Artifact Data Validator

Normalizes artifact.data against its JSON Schema, then validates with jsonschema.

Two result categories:
  corrections  — auto-fixed issues (unknown/type fields removed)
  errors       — schema violations reported by jsonschema

Custom JSON Schema vocabulary (P7-clean — no skill-specific strings in OS code)
------------------------------------------------------------------------------
``x-reyn-members-of: <dotted-path>`` (string)

  Cross-field constraint. The annotated field's value must be a member of
  the set obtained by resolving the dotted path against the validation
  context.  The path syntax is intentionally tiny:

      a.b.c            — descend keys
      a.items[*].name  — for each element of a.items, take element.name

  When the annotation appears on a string field, the field's value must
  equal one of the resolved members. When a field has no value to check
  (e.g. nested under skipped object), the annotation is silently skipped.

  Path resolution that returns an empty set or fails is treated as "no
  constraint" — defensive default; the alternative (reject everything)
  would mask context-plumbing bugs as schema failures.
"""
from __future__ import annotations
from typing import Any

import jsonschema


def extract_data_schema(candidate_schema: dict, artifact_type: str) -> dict:
    """
    Pull the inner data-object schema out of a candidate's artifact_schema.

    candidate_schema shapes:
      - {type, data} wrapper   → return properties.data
      - anyOf [{type,data}, …] → find the variant whose type.const matches artifact_type
      - flat (no data key)     → return the schema as-is
    """
    if "anyOf" in candidate_schema:
        for variant in candidate_schema["anyOf"]:
            const = variant.get("properties", {}).get("type", {}).get("const")
            if const == artifact_type:
                return variant.get("properties", {}).get("data", {})
        return {}

    data_schema = candidate_schema.get("properties", {}).get("data")
    if data_schema is not None:
        return data_schema

    return candidate_schema


def _strip_data(data: dict, schema: dict, corrections: list[str], *, _top_level: bool = True) -> dict:
    """
    Remove keys not declared in schema.properties, plus the injected 'type' key at top level only.
    Recurse into nested objects that have their own properties declaration.
    """
    props = schema.get("properties", {})
    result: dict[str, Any] = {}
    for key, value in data.items():
        # Strip 'type' only at the top level of artifact.data — it's an LLM injection artifact.
        # In nested objects (e.g. items inside an array), 'type' may be a legitimate data field.
        if key == "type" and _top_level:
            corrections.append("removed 'type' from data (injected by LLM)")
            continue
        if props and key not in props:
            corrections.append(f"removed unknown field '{key}'")
            continue
        # Recurse into nested objects
        if isinstance(value, dict) and key in props:
            nested_schema = props[key]
            if nested_schema.get("type") == "object" and "properties" in nested_schema:
                value = _strip_data(value, nested_schema, corrections, _top_level=False)
        # Recurse into arrays of objects
        elif isinstance(value, list) and key in props:
            items_schema = props[key].get("items", {})
            if items_schema.get("type") == "object" and "properties" in items_schema:
                value = [
                    _strip_data(item, items_schema, corrections, _top_level=False)
                    if isinstance(item, dict) else item
                    for item in value
                ]
        result[key] = value
    return result


_MEMBERS_OF_KEYWORD = "x-reyn-members-of"


def _resolve_path(context: dict, path: str) -> tuple[list, bool]:
    """Resolve a tiny dotted path against ``context``.

    Returns ``(members, ok)``. ``ok`` is False when the path is malformed
    or any segment is missing — the caller treats that as "no constraint".

    Supported syntax::

        a.b           — context["a"]["b"]
        a.items[*].n  — [el["n"] for el in context["a"]["items"]]
    """
    if not path:
        return [], False
    segments = path.split(".")
    cur: Any = context
    for i, seg in enumerate(segments):
        if seg.endswith("[*]"):
            key = seg[:-3]
            if not isinstance(cur, dict) or key not in cur:
                return [], False
            arr = cur.get(key)
            if not isinstance(arr, list):
                return [], False
            suffix = ".".join(segments[i + 1 :])
            if not suffix:
                return list(arr), True
            members: list = []
            for el in arr:
                sub_members, sub_ok = _resolve_path({"__el__": el}, "__el__." + suffix)
                if sub_ok:
                    members.extend(sub_members)
            return members, True
        if not isinstance(cur, dict) or seg not in cur:
            return [], False
        cur = cur[seg]
    if isinstance(cur, list):
        return list(cur), True
    return [cur], True


def _check_members_of(
    data: Any,
    schema: dict,
    context: dict,
    errors: list[str],
    path_prefix: str = "",
) -> None:
    """Walk ``schema`` alongside ``data`` and enforce ``x-reyn-members-of``.

    P7 note: this routine knows nothing about specific skills, artifacts,
    or fields — it only interprets the generic vocabulary keyword.
    """
    if not isinstance(schema, dict):
        return
    annotation = schema.get(_MEMBERS_OF_KEYWORD)
    if annotation and isinstance(annotation, str):
        members, ok = _resolve_path(context, annotation)
        if ok and isinstance(data, (str, int, float, bool)) and data not in members:
            label = path_prefix or "data"
            errors.append(
                f"'{label}': value {data!r} is not in {annotation} "
                f"(allowed: {sorted(members) if members else []})"
            )

    # Recurse into properties / items so nested annotations are honored.
    if isinstance(data, dict):
        for key, sub_schema in (schema.get("properties") or {}).items():
            if key in data:
                child_prefix = f"{path_prefix}.{key}" if path_prefix else key
                _check_members_of(data[key], sub_schema, context, errors, child_prefix)
    elif isinstance(data, list):
        items_schema = schema.get("items")
        if isinstance(items_schema, dict):
            for i, item in enumerate(data):
                child_prefix = f"{path_prefix}[{i}]"
                _check_members_of(item, items_schema, context, errors, child_prefix)


def validate_artifact_data(
    artifact: dict,
    candidate_schema: dict,
    strict: bool = False,
    validation_context: dict | None = None,
) -> tuple[dict, list[str], list[str]]:
    """
    Normalize and validate artifact.data against the candidate's JSON Schema.

    Parameters
    ----------
    validation_context
        Optional dict consulted by cross-field constraints (e.g. the
        ``x-reyn-members-of`` annotation). The caller supplies whatever
        keys the schema's annotations reference; a typical mapping is
        ``{"input": <input_artifact>}``. Skill-specific paths live in
        the skill's YAML, never in this OS code (P7).

    Returns
    -------
    (normalized_data, corrections, errors)
      normalized_data — cleaned data dict (unknown fields stripped)
      corrections     — list of auto-fixed issues (informational)
      errors          — jsonschema + cross-field violations; non-empty means invalid
    """
    data = dict(artifact.get("data") or {})
    artifact_type = artifact.get("type", "")
    data_schema = extract_data_schema(candidate_schema, artifact_type)

    corrections: list[str] = []
    errors: list[str] = []

    normalized = _strip_data(data, data_schema, corrections)

    if data_schema:
        validator = jsonschema.Draft7Validator(data_schema)
        for error in sorted(validator.iter_errors(normalized), key=lambda e: list(e.path)):
            path = ".".join(str(p) for p in error.path) if error.path else "data"
            errors.append(f"'{path}': {error.message}")

    if validation_context is not None and data_schema:
        _check_members_of(normalized, data_schema, validation_context, errors)

    return normalized, corrections, errors
