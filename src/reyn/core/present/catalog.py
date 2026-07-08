"""Declarative UI catalog + blueprint structural gate for present (FP-0054).

The v1 catalog is **display-only / non-executable by construction** — the same
philosophy as reyn's structural write-gate: safety from the primitive's *shape*,
not from policy layered on top. A blueprint is data (a component tree), never
code; the LLM authors *labels + path bindings* only.

Catalog (all read-only): ``text`` / ``markdown`` / ``code`` / ``diff`` /
``keyvalue`` / ``table`` / ``list`` / ``image``.

Bindings are expressed structurally as ``{"$bind": "<json-pointer>"}`` — a
single-key object whose value is an RFC 6901 JSON Pointer **string**. This makes
"bindings are path expressions only" enforceable by shape: a ``$bind`` value
that is not a pointer string (an expression, a nested object, a list) is rejected
at parse. Everything that is not a ``$bind`` object is a literal.

The structural gate runs at op validation: a non-catalog component or a non-path
binding is a hard rejection (``PresentBlueprintError``), not a soft drop —
soft-skip is for *runtime* binding misses, not for a malformed blueprint. This gate
is purely structural; leaf-string neutralization (labels, literals, bound values)
is a single seam in the render layer (``binding.resolve_bindings``), not here.
"""
from __future__ import annotations

from typing import Any

# Component name → the set of slot keys it accepts. Slots not listed are rejected
# (keeps the surface tight + the gate exhaustive). ``rows`` / ``columns`` /
# ``items`` carry nested structure validated specially below.
CATALOG: dict[str, frozenset[str]] = {
    "text":     frozenset({"text"}),
    "markdown": frozenset({"text"}),
    "code":     frozenset({"text", "language"}),
    "diff":     frozenset({"text"}),
    "keyvalue": frozenset({"rows"}),
    "table":    frozenset({"rows", "columns"}),
    "list":     frozenset({"items", "item_path"}),
    "image":    frozenset({"src", "alt"}),
}

# The text-family components — a whole-body (plain-text ref) binding may only bind
# into these (§2: structured refs → pointer bindings; plain-text refs → whole-body
# into text-family only).
TEXT_FAMILY: frozenset[str] = frozenset({"text", "markdown", "code", "diff"})

_BIND_KEY = "$bind"


class PresentBlueprintError(ValueError):
    """A blueprint failed the structural gate (non-catalog component / non-path
    binding / malformed slot). A hard rejection — distinct from a runtime binding
    miss, which soft-skips."""


def is_binding(node: Any) -> bool:
    """True iff ``node`` is a binding object ``{"$bind": "<pointer>"}``."""
    return isinstance(node, dict) and set(node.keys()) == {_BIND_KEY}


def binding_pointer(node: dict) -> str:
    """The JSON Pointer string of a binding object (assumes ``is_binding``)."""
    return node[_BIND_KEY]


def _validate_pointer(ptr: Any, *, where: str) -> None:
    """A JSON Pointer (RFC 6901) is a string that is empty (whole-doc) or begins
    with ``/``. Anything else is a non-path binding → hard reject."""
    if not isinstance(ptr, str):
        raise PresentBlueprintError(
            f"{where}: binding must be a JSON-Pointer string, got {type(ptr).__name__}"
        )
    if ptr != "" and not ptr.startswith("/"):
        raise PresentBlueprintError(
            f"{where}: binding {ptr!r} is not a JSON Pointer (must be '' or start with '/')"
        )


def _validate_slot_value(value: Any, *, where: str, allow_bind: bool = True) -> None:
    """A slot value is either a binding object or a literal. A binding is
    validated as a pointer; a literal must be a JSON scalar/str (no nested
    component objects smuggling markup)."""
    if is_binding(value):
        if not allow_bind:
            raise PresentBlueprintError(f"{where}: binding not allowed here")
        _validate_pointer(binding_pointer(value), where=where)
        return
    if isinstance(value, dict):
        # A bare dict that is not a binding is a non-path binding attempt (or an
        # attempt to nest a component where a value belongs) → reject.
        raise PresentBlueprintError(
            f"{where}: expected a literal or a {{'$bind': <pointer>}} binding, got an object"
        )


def _validate_node(node: Any, *, path: str) -> dict:
    """Validate one component node; return a normalized copy (structure only —
    leaf strings are neutralized later at the render seam, not here)."""
    if not isinstance(node, dict):
        raise PresentBlueprintError(f"{path}: component node must be an object, got {type(node).__name__}")
    component = node.get("component")
    if component not in CATALOG:
        raise PresentBlueprintError(
            f"{path}: unknown component {component!r} — not in the display-only catalog "
            f"{sorted(CATALOG)}"
        )
    allowed = CATALOG[component]
    normalized: dict[str, Any] = {"component": component}
    for key, value in node.items():
        if key == "component":
            continue
        if key not in allowed:
            raise PresentBlueprintError(
                f"{path}.{key}: slot {key!r} is not valid for component {component!r} "
                f"(allowed: {sorted(allowed)})"
            )
        normalized[key] = value

    if component in TEXT_FAMILY:
        if "text" in normalized:
            _validate_slot_value(normalized["text"], where=f"{path}.text")
    elif component == "keyvalue":
        normalized["rows"] = _validate_kv_rows(normalized.get("rows"), path=f"{path}.rows")
    elif component == "table":
        _validate_slot_value(normalized.get("rows"), where=f"{path}.rows")
        normalized["columns"] = _validate_columns(normalized.get("columns"), path=f"{path}.columns")
    elif component == "list":
        _validate_slot_value(normalized.get("items"), where=f"{path}.items")
        if "item_path" in normalized:
            _validate_pointer(normalized["item_path"], where=f"{path}.item_path")
    elif component == "image":
        if "src" in normalized:
            _validate_slot_value(normalized["src"], where=f"{path}.src")
    return normalized


def _validate_kv_rows(rows: Any, *, path: str) -> list:
    if not isinstance(rows, list):
        raise PresentBlueprintError(f"{path}: keyvalue rows must be a list of {{label, value}}")
    out = []
    for i, row in enumerate(rows):
        if not isinstance(row, dict) or "label" not in row or "value" not in row:
            raise PresentBlueprintError(f"{path}[{i}]: each keyvalue row needs a label + value")
        _validate_slot_value(row["value"], where=f"{path}[{i}].value")
        out.append({"label": row["label"], "value": row["value"]})
    return out


def _validate_columns(columns: Any, *, path: str) -> list:
    if not isinstance(columns, list):
        raise PresentBlueprintError(f"{path}: table columns must be a list of {{header, path}}")
    out = []
    for i, col in enumerate(columns):
        if not isinstance(col, dict) or "header" not in col or "path" not in col:
            raise PresentBlueprintError(f"{path}[{i}]: each column needs a header + path")
        # A column path is a row-relative JSON Pointer string (not a $bind object).
        _validate_pointer(col["path"], where=f"{path}[{i}].path")
        out.append({"header": col["header"], "path": col["path"]})
    return out


def validate_blueprint(blueprint: Any) -> list[dict]:
    """Structurally gate an inline blueprint → a normalized list of component
    nodes (structure only; leaf-string neutralization happens at the render seam).

    A blueprint is a single component node OR a list of nodes (a top-to-bottom
    sequence — the v1 catalog is display-only with no container component).
    Raises ``PresentBlueprintError`` on any non-catalog component or non-path
    binding.
    """
    if isinstance(blueprint, dict):
        nodes = [blueprint]
    elif isinstance(blueprint, list):
        nodes = blueprint
    else:
        raise PresentBlueprintError(
            f"blueprint must be a component object or a list of them, got {type(blueprint).__name__}"
        )
    if not nodes:
        raise PresentBlueprintError("blueprint is empty — nothing to present")
    return [_validate_node(node, path=f"blueprint[{i}]") for i, node in enumerate(nodes)]
