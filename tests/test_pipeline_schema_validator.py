"""Tier 1: Pipeline schema/type system public API (R2) — registry, validate, resolve_path.

Pins the contract of `reyn.core.pipeline.schema`: nested nested/object/list/
ref field types, `SchemaRegistry.register`/`get`/`has`, `validate()`'s
conforming/error-kind shape, `resolve_path()`'s dotted-path resolution
(including through `list` and `ref`), and the no-recursive-schema rule from
`docs/proposals/reyn-pipeline-v0.9-design-resolutions.md` R2. Uses the
spec's own example schemas (`review`/`feedback`) — no mocks, pure dict/value
fixtures throughout.
"""
from __future__ import annotations

import pytest

from reyn.core.pipeline.schema import (
    SchemaError,
    SchemaRegistry,
    resolve_path,
    validate,
)

# ---------------------------------------------------------------------------
# Fixtures — spec-realistic schemas
# ---------------------------------------------------------------------------

FEEDBACK_SCHEMA = {
    "fields": {
        "file": {"type": "string", "required": True},
        "comment": {"type": "string", "required": True},
    }
}

REVIEW_SCHEMA = {
    "fields": {
        "approved": {"type": "bool", "required": True},
        "feedbacks": {
            "type": "list",
            "of": {"type": "ref", "schema": "feedback"},
            "required": True,
        },
    }
}

FILE_SCHEMA = {
    "fields": {
        "path": {"type": "string", "required": True},
        "risk": {"type": "enum", "values": ["low", "medium", "high"], "required": False},
    }
}

SUSPECTS_SCHEMA = {
    "fields": {
        "suspects": {"type": "list", "of": {"type": "ref", "schema": "file"}, "required": True},
    }
}


@pytest.fixture
def registry() -> SchemaRegistry:
    reg = SchemaRegistry()
    reg.register("feedback", FEEDBACK_SCHEMA)
    reg.register("review", REVIEW_SCHEMA)
    reg.register("file", FILE_SCHEMA)
    reg.register("suspects", SUSPECTS_SCHEMA)
    return reg


# ---------------------------------------------------------------------------
# SchemaRegistry
# ---------------------------------------------------------------------------


def test_register_get_has_roundtrip(registry: SchemaRegistry) -> None:
    """Tier 1: registered schema is retrievable by name; `has` reflects membership."""
    assert registry.has("review")
    assert not registry.has("nonexistent-schema")
    fetched = registry.get("review")
    assert fetched["fields"]["approved"]["type"] == "bool"


def test_get_unknown_schema_raises_keyerror(registry: SchemaRegistry) -> None:
    """Tier 1: `get` on an unregistered name raises KeyError, not a silent None."""
    with pytest.raises(KeyError):
        registry.get("does-not-exist")


@pytest.mark.parametrize(
    "bad_schema",
    [
        {"fields": {}},  # empty fields
        {"fields": {"x": {"type": "bogus-type"}}},  # unknown type
        {"fields": {"x": {"type": "list"}}},  # list missing 'of'
        {"fields": {"x": {"type": "enum", "values": []}}},  # empty enum values
        {"fields": {"x": {"type": "object", "fields": {}}}},  # empty nested object fields
        {"fields": {"x": {"type": "ref"}}},  # ref missing 'schema'
        {"fields": {"x": {"type": "list", "of": {"type": "list", "of": {"type": "string"}}}}},  # list of list
    ],
)
def test_register_rejects_malformed_schema(bad_schema: dict) -> None:
    """Tier 1: malformed FieldType shapes are rejected at registration, not later."""
    reg = SchemaRegistry()
    with pytest.raises(SchemaError):
        reg.register("bad", bad_schema)


def test_register_rejects_self_referential_schema() -> None:
    """Tier 1: a schema referencing itself is rejected (CLAUDE.md R2 — no recursion in v0.9)."""
    reg = SchemaRegistry()
    with pytest.raises(SchemaError):
        reg.register(
            "node",
            {"fields": {"children": {"type": "list", "of": {"type": "ref", "schema": "node"}}}},
        )
    assert not reg.has("node")  # rejected registration leaves no partial state


def test_register_rejects_mutually_recursive_schemas() -> None:
    """Tier 1: A -> ref B -> ref A is a cycle even though neither directly self-refs."""
    reg = SchemaRegistry()
    reg.register("a", {"fields": {"b": {"type": "ref", "schema": "b"}}})
    with pytest.raises(SchemaError):
        reg.register("b", {"fields": {"a": {"type": "ref", "schema": "a"}}})
    # "a" registered fine (no cycle yet at that point); "b" was rejected and rolled back.
    assert reg.has("a")
    assert not reg.has("b")


def test_register_allows_forward_ref_to_not_yet_registered_schema() -> None:
    """Tier 1: registering a ref to an unregistered (not-yet-defined) schema is not a cycle."""
    reg = SchemaRegistry()
    reg.register("review", REVIEW_SCHEMA)  # refs "feedback", not yet registered
    assert reg.has("review")
    assert not reg.has("feedback")
    reg.register("feedback", FEEDBACK_SCHEMA)
    assert reg.has("feedback")


# ---------------------------------------------------------------------------
# validate() — conforming + each error kind
# ---------------------------------------------------------------------------


def test_validate_conforming_nested_value(registry: SchemaRegistry) -> None:
    """Tier 1: a fully-conforming nested value (ref + list of ref) validates clean."""
    value = {
        "approved": True,
        "feedbacks": [
            {"file": "a.py", "comment": "looks good"},
            {"file": "b.py", "comment": "needs work"},
        ],
    }
    result = validate(value, "review", registry)
    assert result.conforming
    assert result.errors == ()


def test_validate_missing_required_field(registry: SchemaRegistry) -> None:
    """Tier 1: a required field absent from the value produces missing_required."""
    result = validate({"feedbacks": []}, "review", registry)
    assert not result.conforming
    assert any(e.kind == "missing_required" and e.path == "approved" for e in result.errors)


def test_validate_type_mismatch_scalar(registry: SchemaRegistry) -> None:
    """Tier 1: wrong scalar type (string where bool expected) is type_mismatch."""
    result = validate({"approved": "yes", "feedbacks": []}, "review", registry)
    assert not result.conforming
    assert any(e.kind == "type_mismatch" and e.path == "approved" for e in result.errors)


def test_validate_bool_not_accepted_as_number() -> None:
    """Tier 1: bool is a distinct scalar from number — True must not satisfy a number field."""
    reg = SchemaRegistry()
    reg.register("s", {"fields": {"n": {"type": "number", "required": True}}})
    result = validate({"n": True}, "s", reg)
    assert not result.conforming
    assert any(e.kind == "type_mismatch" and e.path == "n" for e in result.errors)


def test_validate_enum_not_in_values(registry: SchemaRegistry) -> None:
    """Tier 1: enum value outside declared `values` is enum_invalid."""
    result = validate({"path": "x.py", "risk": "critical"}, "file", registry)
    assert not result.conforming
    assert any(e.kind == "enum_invalid" and e.path == "risk" for e in result.errors)


def test_validate_enum_valid_value_conforms(registry: SchemaRegistry) -> None:
    """Tier 1: enum value within declared `values` conforms."""
    result = validate({"path": "x.py", "risk": "high"}, "file", registry)
    assert result.conforming


def test_validate_list_element_invalid_reports_indexed_path(registry: SchemaRegistry) -> None:
    """Tier 1: an invalid element inside a typed list is reported at its indexed path."""
    value = {
        "approved": True,
        "feedbacks": [
            {"file": "a.py", "comment": "ok"},
            {"file": "b.py"},  # missing required 'comment'
        ],
    }
    result = validate(value, "review", registry)
    assert not result.conforming
    assert any(
        e.kind == "missing_required" and e.path == "feedbacks[1].comment" for e in result.errors
    )


def test_validate_list_wrong_container_type(registry: SchemaRegistry) -> None:
    """Tier 1: a non-list value for a list field is type_mismatch, not a crash."""
    result = validate({"approved": True, "feedbacks": "not-a-list"}, "review", registry)
    assert not result.conforming
    assert any(e.kind == "type_mismatch" and e.path == "feedbacks" for e in result.errors)


def test_validate_unresolved_ref(registry: SchemaRegistry) -> None:
    """Tier 1: a `ref` to a schema absent from the registry is unresolved_ref, not KeyError."""
    reg = SchemaRegistry()
    reg.register("orphan", {"fields": {"thing": {"type": "ref", "schema": "missing-schema"}}})
    result = validate({"thing": {}}, "orphan", reg)
    assert not result.conforming
    assert any(e.kind == "unresolved_ref" and e.path == "thing" for e in result.errors)


def test_validate_nested_object_conforming_and_error() -> None:
    """Tier 1: inline nested `object` fields recurse for both conforming and error cases."""
    reg = SchemaRegistry()
    reg.register(
        "with-address",
        {
            "fields": {
                "address": {
                    "type": "object",
                    "required": True,
                    "fields": {
                        "city": {"type": "string", "required": True},
                        "zip": {"type": "string", "required": False},
                    },
                }
            }
        },
    )
    ok = validate({"address": {"city": "Tokyo"}}, "with-address", reg)
    assert ok.conforming

    bad = validate({"address": {}}, "with-address", reg)
    assert not bad.conforming
    assert any(e.kind == "missing_required" and e.path == "address.city" for e in bad.errors)


def test_validate_root_not_object() -> None:
    """Tier 1: a non-dict root value is reported, not a crash."""
    reg = SchemaRegistry()
    reg.register("s", {"fields": {"x": {"type": "string", "required": True}}})
    result = validate("not-a-dict", "s", reg)
    assert not result.conforming
    assert any(e.kind == "type_mismatch" for e in result.errors)


# ---------------------------------------------------------------------------
# resolve_path()
# ---------------------------------------------------------------------------


def test_resolve_path_top_level_scalar(registry: SchemaRegistry) -> None:
    """Tier 1: single-segment path to a scalar field resolves directly."""
    ft = resolve_path("review", "approved", registry)
    assert ft is not None
    assert ft["type"] == "bool"


def test_resolve_path_list_field_itself(registry: SchemaRegistry) -> None:
    """Tier 1: a path ending AT a list field (e.g. carry_forward target) returns the list FieldType."""
    ft = resolve_path("review", "feedbacks", registry)
    assert ft is not None
    assert ft["type"] == "list"
    assert ft["of"] == {"type": "ref", "schema": "feedback"}


def test_resolve_path_through_list_of_ref_element(registry: SchemaRegistry) -> None:
    """Tier 1: path into a `list of ref` element resolves the element schema's field (N8)."""
    ft = resolve_path("suspects", "suspects.path", registry)
    assert ft is not None
    assert ft == {"type": "string", "required": True}


def test_resolve_path_through_plain_ref(registry: SchemaRegistry) -> None:
    """Tier 1: path through a direct (non-list) `ref` field resolves into the referenced schema."""
    reg = registry
    reg.register(
        "with-file",
        {"fields": {"target": {"type": "ref", "schema": "file", "required": True}}},
    )
    ft = resolve_path("with-file", "target.risk", reg)
    assert ft is not None
    assert ft["type"] == "enum"


def test_resolve_path_through_inline_object(registry: SchemaRegistry) -> None:
    """Tier 1: path through an inline nested `object` field resolves the nested field."""
    reg = registry
    reg.register(
        "wrapper",
        {
            "fields": {
                "meta": {
                    "type": "object",
                    "fields": {"tag": {"type": "string", "required": True}},
                }
            }
        },
    )
    ft = resolve_path("wrapper", "meta.tag", reg)
    assert ft == {"type": "string", "required": True}


def test_resolve_path_invalid_field_name_returns_none(registry: SchemaRegistry) -> None:
    """Tier 1: a path segment that doesn't name a declared field resolves to None."""
    assert resolve_path("review", "nonexistent", registry) is None


def test_resolve_path_descending_past_scalar_returns_none(registry: SchemaRegistry) -> None:
    """Tier 1: a path that tries to descend past a scalar leaf resolves to None."""
    assert resolve_path("review", "approved.nested", registry) is None


def test_resolve_path_descending_past_unresolved_ref_returns_none() -> None:
    """Tier 1: a path descending through a ref to an unregistered schema resolves to None."""
    reg = SchemaRegistry()
    reg.register("orphan", {"fields": {"thing": {"type": "ref", "schema": "missing"}}})
    assert resolve_path("orphan", "thing.anything", reg) is None


# ── #2572: SchemaRegistry.as_dict() / schema_registry_from_dict round-trip ──


def test_schema_registry_as_dict_round_trips_through_serde_with_nested_schema() -> None:
    """Tier 1: ``SchemaRegistry.as_dict()`` ⇄ ``schema_registry_from_dict`` (the
    work-order ``schema_defs`` persistence shape, #2572) round-trips a
    registry with NON-DEFAULT, nested field values (a ``list of ref`` plus an
    ``enum``, not a bare scalar) — the recovery-source shape a crash-resumed
    driver-session must rebuild identically to the original. Also proves the
    intermediate value is JSON-primitive (no custom encoder needed): it
    survives a real ``json.dumps``/``json.loads`` hop."""
    import json

    from reyn.core.pipeline.serde import schema_registry_from_dict

    reg = SchemaRegistry()
    reg.register("file", FILE_SCHEMA)
    reg.register("suspects", SUSPECTS_SCHEMA)

    wire = json.loads(json.dumps(reg.as_dict()))
    rebuilt = schema_registry_from_dict(wire)

    assert rebuilt.has("file") and rebuilt.has("suspects")
    # Validate a conforming AND a non-conforming value against the REBUILT
    # registry to prove the nested ref/list/enum shape survived, not just the
    # top-level keys.
    ok = validate(
        {"suspects": [{"path": "a.py", "risk": "high"}]}, "suspects", rebuilt,
    )
    assert ok.conforming
    bad = validate(
        {"suspects": [{"path": "a.py", "risk": "not-a-risk-level"}]}, "suspects", rebuilt,
    )
    assert not bad.conforming and bad.errors[0].kind == "enum_invalid"


def test_schema_registry_from_dict_none_and_empty_yield_empty_registry() -> None:
    """Tier 1: ``schema_defs=None`` (a work-order with no schemas, or one
    written before this field existed) and ``{}`` both rebuild to an empty,
    usable registry rather than raising."""
    from reyn.core.pipeline.serde import schema_registry_from_dict

    assert schema_registry_from_dict(None).as_dict() == {}
    assert schema_registry_from_dict({}).as_dict() == {}
