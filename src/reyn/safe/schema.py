"""JSON Schema validation helper.

Prefers the optional ``jsonschema`` dependency; falls back to a
minimal in-house implementation supporting ``type``, ``required``,
and ``properties`` only (= sufficient for simple schemas, signals
to the author when they need richer features).
"""

from __future__ import annotations

from typing import Any

try:  # pragma: no cover - import branch determined at install time
    import jsonschema as _jsonschema  # type: ignore
    _HAS_JSONSCHEMA = True
except ImportError:  # pragma: no cover
    _HAS_JSONSCHEMA = False


_TYPE_MAP: dict[str, tuple[type, ...]] = {
    "object": (dict,),
    "array": (list,),
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "null": (type(None),),
}


class SchemaError(ValueError):
    """Raised when ``data`` does not conform to ``schema``."""


def _minimal_validate(data: Any, schema: dict, path: str = "$") -> None:
    expected = schema.get("type")
    if expected is not None:
        # ``integer`` is a subset of ``number`` but bool subclasses int —
        # exclude bool from int/number checks.
        types = _TYPE_MAP.get(expected)
        if types is None:
            raise SchemaError(f"{path}: unknown schema type {expected!r}")
        if expected in ("integer", "number") and isinstance(data, bool):
            raise SchemaError(f"{path}: expected {expected}, got bool")
        if not isinstance(data, types):
            raise SchemaError(
                f"{path}: expected {expected}, got {type(data).__name__}"
            )
    if expected == "object" or (expected is None and isinstance(data, dict)):
        required = schema.get("required", [])
        for key in required:
            if key not in data:
                raise SchemaError(f"{path}: missing required property {key!r}")
        properties = schema.get("properties", {})
        for key, subschema in properties.items():
            if key in data:
                _minimal_validate(data[key], subschema, f"{path}.{key}")


def validate(data: Any, schema: dict) -> None:
    """Validate ``data`` against the JSON Schema ``schema``.

    Raises ``SchemaError`` (a ``ValueError`` subclass) if invalid.
    Uses the ``jsonschema`` package if available, otherwise a
    minimal fallback that understands ``type`` / ``required`` /
    ``properties``.
    """
    if _HAS_JSONSCHEMA:
        try:
            _jsonschema.validate(instance=data, schema=schema)  # type: ignore[attr-defined]
        except _jsonschema.ValidationError as exc:  # type: ignore[attr-defined]
            raise SchemaError(str(exc)) from exc
    else:
        _minimal_validate(data, schema)
