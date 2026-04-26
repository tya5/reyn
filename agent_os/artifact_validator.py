"""
Artifact Data Validator

Normalizes and validates artifact.data against a compiled JSON Schema.
Runs after {type, data} structure is ensured, before the artifact is stored.

Two distinct result categories:
  corrections  — auto-fixed issues (extra fields removed, type coerced)
  errors       — unfixable issues (required field missing, incompatible type)

Callers treat corrections as informational and errors as failures.
"""
from __future__ import annotations
from typing import Any


# ── Schema extraction ──────────────────────────────────────────────────────────

def extract_data_schema(candidate_schema: dict, artifact_type: str) -> dict:
    """
    Pull the inner data-object schema out of a candidate's artifact_schema.

    candidate_schema is one of:
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

    # Flat schema (no wrapper) — use as-is
    return candidate_schema


# ── Type coercion ──────────────────────────────────────────────────────────────

def _coerce(value: Any, expected_type: str, path: str, corrections: list[str]) -> Any:
    """
    Attempt to coerce value to the JSON Schema primitive type.
    Returns the (possibly converted) value.
    Raises TypeError if coercion is impossible.
    """
    if expected_type == "number":
        if isinstance(value, bool):
            raise TypeError(f"'{path}': expected number, got boolean")
        if isinstance(value, (int, float)):
            return value
        if isinstance(value, str):
            try:
                coerced = float(value)
                corrections.append(f"coerced '{path}' from string to number")
                return coerced
            except ValueError:
                raise TypeError(f"'{path}': cannot coerce '{value!r}' to number")
        raise TypeError(f"'{path}': expected number, got {type(value).__name__}")

    if expected_type == "integer":
        if isinstance(value, bool):
            raise TypeError(f"'{path}': expected integer, got boolean")
        if isinstance(value, int):
            return value
        if isinstance(value, float) and value.is_integer():
            coerced = int(value)
            corrections.append(f"coerced '{path}' from float to integer")
            return coerced
        if isinstance(value, str):
            try:
                coerced = int(value)
                corrections.append(f"coerced '{path}' from string to integer")
                return coerced
            except ValueError:
                raise TypeError(f"'{path}': cannot coerce '{value!r}' to integer")
        raise TypeError(f"'{path}': expected integer, got {type(value).__name__}")

    if expected_type == "boolean":
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.lower() in ("true", "false"):
            coerced = value.lower() == "true"
            corrections.append(f"coerced '{path}' from string to boolean")
            return coerced
        raise TypeError(f"'{path}': expected boolean, got {type(value).__name__}")

    if expected_type == "string":
        if isinstance(value, str):
            return value
        raise TypeError(f"'{path}': expected string, got {type(value).__name__}")

    if expected_type == "array":
        if isinstance(value, list):
            return value
        raise TypeError(f"'{path}': expected array, got {type(value).__name__}")

    if expected_type == "object":
        if isinstance(value, dict):
            return value
        raise TypeError(f"'{path}': expected object, got {type(value).__name__}")

    return value  # unknown / unconstrained — pass through


# ── Core recursive normalizer/validator ───────────────────────────────────────

def _process(
    data: dict,
    schema: dict,
    path: str,
    corrections: list[str],
    errors: list[str],
) -> dict:
    """
    Recursively normalize and validate a data dict against a JSON Schema object.

    - Strips unknown fields (lenient)
    - Strips 'type' contamination from data
    - Coerces compatible scalar types
    - Recurses into nested objects and array items
    - Records required-field misses in errors
    """
    props: dict[str, dict] = schema.get("properties", {})
    required: set[str] = set(schema.get("required", []))
    result: dict[str, Any] = {}

    def field_path(key: str) -> str:
        return f"{path}.{key}" if path else key

    # Pass 1: drop unknown and contaminating fields
    for key, val in data.items():
        if key == "type":
            corrections.append(f"removed 'type' from '{path or 'data'}'")
            continue
        if props and key not in props:
            corrections.append(f"removed unknown field '{field_path(key)}'")
            continue
        result[key] = val

    # Pass 2: check required presence
    for req in required:
        if req not in result:
            errors.append(f"required field '{field_path(req)}' is missing")

    # Pass 3: type-check and coerce each present field
    for key, field_schema in props.items():
        if key not in result:
            continue  # missing optional fields are fine; required already recorded above

        value = result[key]
        fp = field_path(key)
        field_type = field_schema.get("type")

        if field_type == "array":
            if not isinstance(value, list):
                try:
                    value = _coerce(value, "array", fp, corrections)
                except TypeError as exc:
                    errors.append(str(exc))
                    continue

            items_schema = field_schema.get("items", {})
            items_type = items_schema.get("type")
            coerced_items: list[Any] = []
            for i, item in enumerate(value):
                ip = f"{fp}[{i}]"
                if items_type == "object" and isinstance(item, dict):
                    coerced_items.append(_process(item, items_schema, ip, corrections, errors))
                elif items_type:
                    try:
                        coerced_items.append(_coerce(item, items_type, ip, corrections))
                    except TypeError as exc:
                        errors.append(str(exc))
                        coerced_items.append(item)
                else:
                    coerced_items.append(item)
            result[key] = coerced_items

        elif field_type == "object":
            if isinstance(value, dict):
                result[key] = _process(value, field_schema, fp, corrections, errors)
            else:
                errors.append(f"'{fp}': expected object, got {type(value).__name__}")

        elif field_type:
            try:
                result[key] = _coerce(value, field_type, fp, corrections)
            except TypeError as exc:
                errors.append(str(exc))

    return result


# ── Public API ─────────────────────────────────────────────────────────────────

def validate_artifact_data(
    artifact: dict,
    candidate_schema: dict,
) -> tuple[dict, list[str], list[str]]:
    """
    Normalize and validate artifact.data against the candidate's schema.

    Parameters
    ----------
    artifact        : the normalized {type, data} artifact dict
    candidate_schema: the full artifact_schema from CandidateOutput

    Returns
    -------
    (normalized_data, corrections, errors)
      normalized_data — cleaned, coerced data dict to replace artifact["data"]
      corrections     — list of auto-fixed issues (informational)
      errors          — list of unfixable issues; non-empty means the artifact is invalid
    """
    data = dict(artifact.get("data") or {})
    artifact_type = artifact.get("type", "")

    data_schema = extract_data_schema(candidate_schema, artifact_type)

    corrections: list[str] = []
    errors: list[str] = []

    normalized = _process(data, data_schema, "", corrections, errors)
    return normalized, corrections, errors
