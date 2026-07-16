"""Pipeline schema/type registry, validator, and static path resolver (R2).

Implements R2 of `docs/proposals/reyn-pipeline-v0.9-design-resolutions.md`:
a nested, monomorphic (no generics) type system for Pipeline step outputs.
A `Schema` names a set of `FieldType`s; a `FieldType` is a scalar
(`bool`/`string`/`number`), an `enum`, a typed `list` (`of` is mandatory —
no untyped lists), an inline nested `object`, or a `ref` to another
registered schema. This is what makes `match.on` / `until` / `carry_forward`
/ `for_each over` paths statically resolvable (spec rule 7 / N8) instead of
free-form dict navigation.

Three pieces, all pure / IO-free (a YAML-backed loader belongs in a
separate thin wrapper, not here):

  - `SchemaRegistry` — register/get named schemas by dict. Registration
    validates the schema's own shape (known field types, `list.of` present,
    `enum.values` non-empty, ...) and rejects a **recursive schema**: v0.9
    schemas may not reference themselves transitively via `ref` (this is
    what keeps `validate`/`resolve_path` total — no risk of infinite
    descent). Detection runs a cycle check over the ref-dependency graph
    of all currently-registered schemas after each registration and rolls
    back on cycle.
  - `validate(value, schema, registry) -> ValidationResult` — recursively
    checks a value against a schema (or schema name), producing a list of
    typed `ValidationError`s (`missing_required` / `type_mismatch` /
    `enum_invalid` / `unresolved_ref` / `unknown_type` / `out_of_range`)
    rather than raising, so callers can report every violation instead of
    just the first.
  - `resolve_path(schema, "dotted.path", registry) -> FieldType | None` —
    walks a dotted path through a (possibly nested) schema, transparently
    unwrapping `object` fields, `ref` fields (via the registry), and `list`
    fields (into their `of` element type) at each intermediate segment, so
    a path like `"suspects.path"` against `suspects: list of ref(file)`
    resolves through the list into `file`'s `path` field. Returns `None`
    for any path that doesn't resolve — this is the seam the pipeline
    static analyzer uses to validate `match.on` / `until` / `carry_forward`
    / `for_each over` path references.

ElemType (the type allowed inside `list.of`) intentionally excludes `list`
itself — no lists-of-lists — matching the grammar in R2 / appendix B; this
is enforced at registration time, not just documented.

`number` range constraints (#2963): a `number` FieldType may additionally
carry `minimum:` and/or `maximum:` (inclusive bounds). Before this, the DSL
could only ever say `{type: number}` — a schema author's "score in [0.0,
1.0]" lived in a prompt comment or a docstring, not in the schema, so a
model answering `85` (a 0-100 scale) against a `>= 0.6` threshold check
passed unchallenged. `minimum`/`maximum` are validated (at registration,
`minimum <= maximum` if both are given; at value-validation, a value
outside the bounds is an `out_of_range` error) and propagated into
`to_json_schema`'s output, so a `response_format`-constrained `agent` step
has the bound enforced at BOTH generation time (provider-side JSON Schema
constraint) and post-hoc validation time — not just one or the other.
Scoped to `number` only: `bool`/`string` have no natural "range", and a
`string` length cap / `list`/`array` element-count cap are a separate,
not-yet-demonstrated need (not added here — see #2963 PR discussion).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# Field-type vocabulary
# ---------------------------------------------------------------------------

SCALAR_TYPES = frozenset({"bool", "string", "number"})
_KNOWN_TYPES = SCALAR_TYPES | {"enum", "list", "object", "ref"}

# Sentinel distinguishing "key absent from the value dict" from "key present
# with an explicit None" — only the former is a missing-required-field error;
# the latter is a type mismatch against every current field type.
_MISSING = object()


class SchemaError(ValueError):
    """Raised when a schema registration is malformed or recursive."""


# ---------------------------------------------------------------------------
# Validation result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ValidationError:
    """One violation found while validating a value against a schema.

    `path` is the dotted/indexed path to the offending value (e.g.
    `"feedbacks[1].comment"`, `""` for the root). `kind` is one of
    `missing_required` / `type_mismatch` / `enum_invalid` / `unresolved_ref`
    / `unknown_type` / `out_of_range` — stable tokens callers may branch on.
    """

    path: str
    message: str
    kind: str


@dataclass(frozen=True)
class ValidationResult:
    """Outcome of `validate()`: either conforming, or a list of errors."""

    conforming: bool
    errors: tuple[ValidationError, ...] = ()

    @classmethod
    def ok(cls) -> ValidationResult:
        return cls(conforming=True, errors=())

    @classmethod
    def fail(cls, errors: list[ValidationError]) -> ValidationResult:
        return cls(conforming=False, errors=tuple(errors))


# ---------------------------------------------------------------------------
# Schema registry
# ---------------------------------------------------------------------------


@dataclass
class SchemaRegistry:
    """Named-schema store. Register with a plain dict; refs resolve by name.

    Decoupled from disk on purpose (per R2 / task scope) — a `.reyn/schemas/
    <name>.yaml` loader can sit in front of this and call `register()` per
    file; the registry itself never touches IO.
    """

    _schemas: dict[str, dict[str, Any]] = field(default_factory=dict)

    def register(self, name: str, schema: dict[str, Any]) -> None:
        """Register `schema` under `name`.

        Raises `SchemaError` if the schema's own shape is malformed, or if
        registering it would introduce a recursive `ref` cycle (self- or
        mutually-referential) among currently-registered schemas.
        """
        _check_schema_shape(name, schema)
        normalized = dict(schema)
        normalized.setdefault("name", name)

        previous = self._schemas.get(name)
        self._schemas[name] = normalized
        cycle = _find_ref_cycle(self._schemas)
        if cycle is not None:
            if previous is None:
                del self._schemas[name]
            else:
                self._schemas[name] = previous
            raise SchemaError(
                f"recursive schema detected: {' -> '.join(cycle)}"
            )

    def get(self, name: str) -> dict[str, Any]:
        if name not in self._schemas:
            raise KeyError(f"unknown schema: {name!r}")
        return self._schemas[name]

    def has(self, name: str) -> bool:
        return name in self._schemas

    def as_dict(self) -> "dict[str, dict[str, Any]]":
        """A plain ``name -> schema dict`` snapshot of every registered schema
        (#2572: the work-order persistence shape — see
        ``reyn.core.pipeline.serde.schema_registry_from_dict`` for the
        inverse). A PUBLIC accessor over the already-JSON-primitive
        ``_schemas`` values, so a caller needs no private-state reach to
        serialize a registry."""
        return dict(self._schemas)


# ---------------------------------------------------------------------------
# Schema-shape validation (registration-time — catches malformed schemas
# early, independent of any value being validated against them)
# ---------------------------------------------------------------------------


def _check_field_type_shape(ft: Any, path: str, *, allow_list: bool) -> None:
    if not isinstance(ft, dict):
        raise SchemaError(f"{path}: field type must be a dict, got {type(ft).__name__}")
    t = ft.get("type")
    if t in SCALAR_TYPES:
        if t == "number":
            _check_number_bounds_shape(ft, path)
        return
    if t == "enum":
        values = ft.get("values")
        if not isinstance(values, list) or not values:
            raise SchemaError(f"{path}: enum type requires non-empty 'values'")
        return
    if t == "list":
        if not allow_list:
            raise SchemaError(f"{path}: list element type may not itself be a list")
        of = ft.get("of")
        if of is None:
            raise SchemaError(f"{path}: list type requires 'of' (no untyped lists)")
        _check_field_type_shape(of, f"{path}.of", allow_list=False)
        return
    if t == "object":
        fields = ft.get("fields")
        if not isinstance(fields, dict) or not fields:
            raise SchemaError(f"{path}: object type requires non-empty 'fields'")
        for fname, sub in fields.items():
            _check_field_type_shape(sub, f"{path}.{fname}", allow_list=True)
        return
    if t == "ref":
        if not ft.get("schema"):
            raise SchemaError(f"{path}: ref type requires 'schema'")
        return
    raise SchemaError(f"{path}: unknown field type {t!r} (expected one of {sorted(_KNOWN_TYPES)})")


def _check_number_bounds_shape(ft: dict[str, Any], path: str) -> None:
    """Validate a `number` FieldType's optional `minimum`/`maximum` bounds
    (#2963). Each, if present, must be an `int`/`float` (not `bool` — same
    exclusion `_validate_field` applies to the value itself); if both are
    present, `minimum` must not exceed `maximum` — an inverted range can
    never be satisfied by any value, so it is rejected at registration
    time rather than silently discarding every value later."""
    minimum = ft.get("minimum")
    maximum = ft.get("maximum")
    for key, bound in (("minimum", minimum), ("maximum", maximum)):
        if bound is not None and (isinstance(bound, bool) or not isinstance(bound, (int, float))):
            raise SchemaError(f"{path}: '{key}' must be a number, got {type(bound).__name__}")
    if minimum is not None and maximum is not None and minimum > maximum:
        raise SchemaError(f"{path}: 'minimum' ({minimum!r}) must not exceed 'maximum' ({maximum!r})")


def _check_schema_shape(name: str, schema: Any) -> None:
    if not isinstance(schema, dict):
        raise SchemaError(f"schema {name!r} must be a dict")
    fields = schema.get("fields")
    if not isinstance(fields, dict) or not fields:
        raise SchemaError(f"schema {name!r} requires non-empty 'fields'")
    for fname, ft in fields.items():
        _check_field_type_shape(ft, f"{name}.{fname}", allow_list=True)


# ---------------------------------------------------------------------------
# Recursive-schema detection (v0.9: no self- or mutually-referential schemas)
# ---------------------------------------------------------------------------


def _direct_refs(ft: dict[str, Any]) -> set[str]:
    """`ref` schema names directly reachable from one FieldType."""
    t = ft.get("type")
    if t == "ref":
        return {ft["schema"]}
    if t == "list":
        return _direct_refs(ft["of"])
    if t == "object":
        refs: set[str] = set()
        for sub in ft["fields"].values():
            refs |= _direct_refs(sub)
        return refs
    return set()


def _schema_direct_refs(schema: dict[str, Any]) -> set[str]:
    refs: set[str] = set()
    for ft in schema["fields"].values():
        refs |= _direct_refs(ft)
    return refs


def _find_ref_cycle(schemas: dict[str, dict[str, Any]]) -> list[str] | None:
    """DFS cycle detection over the ref-dependency graph of `schemas`.

    Edges to a schema name not (yet) present in `schemas` are ignored —
    an unresolved forward ref is not itself a cycle; it surfaces as an
    `unresolved_ref` validation error instead, only if it never resolves.
    Returns the cycle as a list of schema names (closed loop, first ==
    last) if one exists, else None.
    """
    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[str, int] = {name: WHITE for name in schemas}
    stack: list[str] = []

    def dfs(node: str) -> list[str] | None:
        color[node] = GRAY
        stack.append(node)
        for ref in sorted(_schema_direct_refs(schemas[node])):
            if ref not in schemas:
                continue
            if color[ref] == GRAY:
                idx = stack.index(ref)
                return [*stack[idx:], ref]
            if color[ref] == WHITE:
                found = dfs(ref)
                if found is not None:
                    return found
        stack.pop()
        color[node] = BLACK
        return None

    for name in schemas:
        if color[name] == WHITE:
            found = dfs(name)
            if found is not None:
                return found
    return None


# ---------------------------------------------------------------------------
# Value validation
# ---------------------------------------------------------------------------


def validate(
    value: Any, schema: dict[str, Any] | str, registry: SchemaRegistry
) -> ValidationResult:
    """Validate `value` against `schema` (a schema dict, or a registered name).

    Recurses through nested `object`/`list`/`ref` fields. Returns a
    `ValidationResult` — never raises for a non-conforming value (only
    malformed *schemas*, caught earlier at registration, raise).
    """
    if isinstance(schema, str):
        schema = registry.get(schema)
    errors: list[ValidationError] = []
    if not isinstance(value, dict):
        errors.append(
            ValidationError("", f"expected object, got {type(value).__name__}", "type_mismatch")
        )
        return ValidationResult.fail(errors)
    _validate_object_fields(value, schema["fields"], "", registry, errors)
    return ValidationResult(conforming=not errors, errors=tuple(errors))


def _validate_object_fields(
    value: dict[str, Any],
    fields: dict[str, dict[str, Any]],
    path: str,
    registry: SchemaRegistry,
    errors: list[ValidationError],
) -> None:
    for fname, ft in fields.items():
        fpath = f"{path}.{fname}" if path else fname
        fvalue = value.get(fname, _MISSING)
        if fvalue is _MISSING:
            if ft.get("required"):
                errors.append(ValidationError(fpath, "required field missing", "missing_required"))
            continue
        _validate_field(fvalue, ft, fpath, registry, errors)


def _validate_number_bounds(
    value: Any, ft: dict[str, Any], path: str, errors: list[ValidationError]
) -> None:
    """Enforce a `number` FieldType's `minimum`/`maximum` (#2963), both
    inclusive. Runs only after the caller has already confirmed `value` is a
    non-bool `int`/`float` — this is the closing half of the "0.6 threshold
    against a model answering 85 on a 0-100 scale" gap: `to_json_schema`
    asks the provider to constrain generation to the same bound, but a
    provider that ignores/can't honor it (or a non-`agent` `tool`/`shell`
    step) still gets caught here, post-hoc."""
    minimum = ft.get("minimum")
    maximum = ft.get("maximum")
    if minimum is not None and value < minimum:
        errors.append(
            ValidationError(path, f"{value!r} is below minimum {minimum!r}", "out_of_range")
        )
    if maximum is not None and value > maximum:
        errors.append(
            ValidationError(path, f"{value!r} is above maximum {maximum!r}", "out_of_range")
        )


def _validate_field(
    value: Any,
    ft: dict[str, Any],
    path: str,
    registry: SchemaRegistry,
    errors: list[ValidationError],
) -> None:
    t = ft.get("type")
    if t in SCALAR_TYPES:
        ok = {
            "bool": isinstance(value, bool),
            "string": isinstance(value, str),
            "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        }[t]
        if not ok:
            errors.append(ValidationError(path, f"expected {t}, got {type(value).__name__}", "type_mismatch"))
            return
        if t == "number":
            _validate_number_bounds(value, ft, path, errors)
        return
    if t == "enum":
        if value not in ft["values"]:
            errors.append(ValidationError(path, f"{value!r} not in {ft['values']}", "enum_invalid"))
        return
    if t == "list":
        if not isinstance(value, list):
            errors.append(ValidationError(path, f"expected list, got {type(value).__name__}", "type_mismatch"))
            return
        for i, item in enumerate(value):
            _validate_field(item, ft["of"], f"{path}[{i}]", registry, errors)
        return
    if t == "object":
        if not isinstance(value, dict):
            errors.append(ValidationError(path, f"expected object, got {type(value).__name__}", "type_mismatch"))
            return
        _validate_object_fields(value, ft["fields"], path, registry, errors)
        return
    if t == "ref":
        ref_name = ft["schema"]
        if not registry.has(ref_name):
            errors.append(ValidationError(path, f"unresolved ref: {ref_name}", "unresolved_ref"))
            return
        if not isinstance(value, dict):
            errors.append(
                ValidationError(path, f"expected object (ref {ref_name}), got {type(value).__name__}", "type_mismatch")
            )
            return
        _validate_object_fields(value, registry.get(ref_name)["fields"], path, registry, errors)
        return
    errors.append(ValidationError(path, f"unknown field type: {t!r}", "unknown_type"))


# ---------------------------------------------------------------------------
# Static path resolution
# ---------------------------------------------------------------------------


def _fields_of(ft: dict[str, Any], registry: SchemaRegistry) -> dict[str, dict[str, Any]] | None:
    """The nested field map reachable by descending one more path segment
    past `ft`, transparently unwrapping `object` / `ref` / `list`.

    Returns None for scalars/enums (nothing further to descend into) or
    an unresolved ref.
    """
    t = ft.get("type")
    if t == "object":
        return ft["fields"]
    if t == "ref":
        ref_name = ft["schema"]
        if not registry.has(ref_name):
            return None
        return registry.get(ref_name)["fields"]
    if t == "list":
        return _fields_of(ft["of"], registry)
    return None


def to_json_schema(schema: dict[str, Any] | str, registry: SchemaRegistry) -> dict[str, Any]:
    """Convert a reyn pipeline ``Schema`` (this module's own field-type
    vocabulary — ``bool``/``string``/``number``/``enum``/``list``/``object``/
    ``ref``, see the module docstring) into a standard JSON Schema object
    dict, suitable for an LLM provider's ``response_format={"type":
    "json_schema", "json_schema": {"schema": ...}}`` (0062 §2.1).

    Passed VERBATIM by the caller — this function does no strict-mode
    augmentation (no injected ``"strict": true`` / ``"additionalProperties":
    false`` / optional-field coercion; 0062 §2.3 pin). A provider that
    rejects the resulting schema surfaces that as the caller's own
    failure-mode-(b) typed error — augmentation, if ever added, is a
    deliberate v2 follow-up, not a silent default here.

    ``ref`` fields are expanded INLINE (not ``$ref``/``$defs`` — providers'
    json_schema subsets vary in ``$ref`` support, and inlining sidesteps that
    entirely). This always terminates: ``SchemaRegistry.register`` already
    rejects a recursive schema (self- or mutually-referential ``ref``) at
    registration time, so no ``ref`` chain reachable from a registered schema
    can cycle.
    """
    if isinstance(schema, str):
        schema = registry.get(schema)
    return _fields_to_json_schema(schema["fields"], registry)


def _fields_to_json_schema(
    fields: dict[str, dict[str, Any]], registry: SchemaRegistry
) -> dict[str, Any]:
    properties: dict[str, Any] = {}
    required: list[str] = []
    for fname, ft in fields.items():
        properties[fname] = _field_type_to_json_schema(ft, registry)
        if ft.get("required"):
            required.append(fname)
    out: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        out["required"] = required
    return out


def _field_type_to_json_schema(ft: dict[str, Any], registry: SchemaRegistry) -> dict[str, Any]:
    t = ft.get("type")
    if t == "bool":
        return {"type": "boolean"}
    if t == "string":
        return {"type": "string"}
    if t == "number":
        out: dict[str, Any] = {"type": "number"}
        if ft.get("minimum") is not None:
            out["minimum"] = ft["minimum"]
        if ft.get("maximum") is not None:
            out["maximum"] = ft["maximum"]
        return out
    if t == "enum":
        return {"enum": list(ft["values"])}
    if t == "list":
        return {"type": "array", "items": _field_type_to_json_schema(ft["of"], registry)}
    if t == "object":
        return _fields_to_json_schema(ft["fields"], registry)
    if t == "ref":
        return _fields_to_json_schema(registry.get(ft["schema"])["fields"], registry)
    raise SchemaError(f"to_json_schema: unknown field type {t!r}")


def resolve_path(
    schema: dict[str, Any] | str, path: str, registry: SchemaRegistry
) -> dict[str, Any] | None:
    """Resolve a dotted path through `schema`, returning the FieldType at
    that path, or None if the path doesn't exist.

    Intermediate `list` fields are transparently unwrapped into their `of`
    element type (so `"suspects.path"` against `suspects: {type: list, of:
    {type: ref, schema: file}}` resolves through the list into `file`'s
    `path` field) — this is the seam `for_each over` / `match.on` / `until`
    / `carry_forward` path checks use.
    """
    if isinstance(schema, str):
        schema = registry.get(schema)
    parts = path.split(".")
    fields = schema["fields"]
    current: dict[str, Any] | None = None
    for i, part in enumerate(parts):
        if part not in fields:
            return None
        current = fields[part]
        if i == len(parts) - 1:
            return current
        nested = _fields_of(current, registry)
        if nested is None:
            return None
        fields = nested
    return current
