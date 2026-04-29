"""
Artifact Data Validator

Normalizes artifact.data against its JSON Schema, then validates with jsonschema.

Two result categories:
  corrections  — auto-fixed issues (unknown/type fields removed)
  errors       — schema violations reported by jsonschema
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


def validate_artifact_data(
    artifact: dict,
    candidate_schema: dict,
    strict: bool = False,
) -> tuple[dict, list[str], list[str]]:
    """
    Normalize and validate artifact.data against the candidate's JSON Schema.

    Returns
    -------
    (normalized_data, corrections, errors)
      normalized_data — cleaned data dict (unknown fields stripped)
      corrections     — list of auto-fixed issues (informational)
      errors          — jsonschema validation errors; non-empty means invalid
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

    return normalized, corrections, errors
