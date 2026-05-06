"""Tier 2 OS invariant tests for anyOf/oneOf/allOf handling in preprocessor_typing.

Guards the B7-S5b fix: _get_at_path must traverse anyOf/oneOf/allOf branches
so that union input schemas (e.g. user_message | eval_builder_request) do not
cause PreprocessorTypeError at compile time when a preprocessor step writes
into a path that exists in at least one branch.

Invariants tested:
  (a) anyOf with path present in one branch only — resolves successfully
  (b) anyOf with path present in both branches — resolves to first branch match
  (c) anyOf nested inside a properties value — full dotted path traversal works
  (d) oneOf behaves identically to anyOf (same semantics for path resolution)
  (e) allOf: path present in one of the constraints — resolves successfully
  (f) anyOf where no branch contains the path — raises PreprocessorTypeError
  (g) ordinary object schema (no union keywords) — behaviour unchanged (regression)
  (h) eval_builder real schema (user_message | eval_builder_request) —
      _require_parent_exists accepts 'data' as parent for 'data._prep'

Testing policy (docs/ja/contributing/testing.md):
  - No mocks (real schema dicts + real functions)
  - No private-state assertions
  - No algorithm-level pins
"""
from __future__ import annotations

import pytest

from reyn.compiler.preprocessor_typing import (
    PreprocessorTypeError,
    _get_at_path,
    _require_parent_exists,
)

# ── schema builders ────────────────────────────────────────────────────────────


def _obj(properties: dict, required: list[str] | None = None) -> dict:
    s: dict = {"type": "object", "properties": properties}
    if required:
        s["required"] = required
    return s


def _str_schema() -> dict:
    return {"type": "string"}


def _branch_with_data_x() -> dict:
    """Object schema with properties.data.properties.x."""
    return _obj({"data": _obj({"x": _str_schema()})})


def _branch_without_data() -> dict:
    """Object schema that has no 'data' property."""
    return _obj({"text": _str_schema()})


def _branch_with_data_y() -> dict:
    """Object schema with properties.data.properties.y (but not x)."""
    return _obj({"data": _obj({"y": _str_schema()})})


# ── (a) anyOf: path in exactly one branch ─────────────────────────────────────


def test_anyof_path_in_one_branch_resolves():
    """Tier 2: _get_at_path resolves when path exists in exactly one anyOf branch.

    Branch 0 has no 'data' property; branch 1 has 'data.x'. The lookup for
    'data.x' must succeed by falling through to branch 1.
    """
    schema = {"anyOf": [_branch_without_data(), _branch_with_data_x()]}
    result = _get_at_path(schema, "data.x")
    assert result == _str_schema()


# ── (b) anyOf: path in both branches ──────────────────────────────────────────


def test_anyof_path_in_both_branches_resolves():
    """Tier 2: _get_at_path resolves when path exists in both anyOf branches.

    When multiple branches contain the path, the first match is returned
    without error. The exact branch does not matter for correctness.
    """
    branch_a = _obj({"data": _obj({"x": _str_schema()})})
    branch_b = _obj({"data": _obj({"x": {"type": "integer"}})})
    schema = {"anyOf": [branch_a, branch_b]}
    result = _get_at_path(schema, "data.x")
    # Any truthy dict result indicates success; we do not pin which branch wins.
    assert isinstance(result, dict)


# ── (c) anyOf nested inside a properties value ────────────────────────────────


def test_anyof_nested_inside_properties_resolves():
    """Tier 2: _get_at_path resolves a dotted path through a union nested in properties.

    Schema shape: {properties: {input: {anyOf: [{properties: {data: {properties: {x: ...}}}}, ...]}}}
    Path 'input.data.x' must be resolved by descending into input, then into the anyOf.
    """
    inner_branch = _obj({"data": _obj({"x": _str_schema()})})
    inner_no_data = _obj({"other": _str_schema()})
    schema = _obj({"input": {"anyOf": [inner_no_data, inner_branch]}})
    result = _get_at_path(schema, "input.data.x")
    assert result == _str_schema()


# ── (d) oneOf behaves identically to anyOf ────────────────────────────────────


def test_oneof_path_in_one_branch_resolves():
    """Tier 2: _get_at_path resolves when path exists in exactly one oneOf branch.

    oneOf and anyOf have the same traversal semantics for path lookup.
    """
    schema = {"oneOf": [_branch_without_data(), _branch_with_data_x()]}
    result = _get_at_path(schema, "data.x")
    assert result == _str_schema()


def test_oneof_no_branch_raises():
    """Tier 2: _get_at_path raises PreprocessorTypeError when no oneOf branch matches."""
    schema = {"oneOf": [_branch_without_data(), _obj({"other": _str_schema()})]}
    with pytest.raises(PreprocessorTypeError):
        _get_at_path(schema, "data.x")


# ── (e) allOf: path present in at least one constraint ────────────────────────


def test_allof_path_in_one_constraint_resolves():
    """Tier 2: _get_at_path resolves when path exists in one of the allOf constraints.

    allOf merges constraints; if any constraint declares the property the path
    is considered reachable.
    """
    base = _obj({"type": _str_schema()})          # no 'data'
    extension = _obj({"data": _obj({"x": _str_schema()})})
    schema = {"allOf": [base, extension]}
    result = _get_at_path(schema, "data.x")
    assert result == _str_schema()


# ── (f) no branch contains the path → raises ──────────────────────────────────


def test_anyof_no_branch_raises():
    """Tier 2: _get_at_path raises PreprocessorTypeError when no anyOf branch has the path."""
    schema = {"anyOf": [_branch_without_data(), _obj({"other": _str_schema()})]}
    with pytest.raises(PreprocessorTypeError):
        _get_at_path(schema, "data.x")


def test_allof_no_branch_raises():
    """Tier 2: _get_at_path raises PreprocessorTypeError when no allOf branch has the path."""
    schema = {"allOf": [_obj({"a": _str_schema()}), _obj({"b": _str_schema()})]}
    with pytest.raises(PreprocessorTypeError):
        _get_at_path(schema, "data.x")


# ── (g) ordinary object schema — no regression ────────────────────────────────


def test_plain_object_schema_resolves_unchanged():
    """Tier 2: _get_at_path on a plain object schema (no anyOf/oneOf/allOf) is unchanged.

    Regression guard: the fix must not break existing non-union schema traversal.
    """
    schema = _obj({"data": _obj({"target_skill": _str_schema(), "extra": _str_schema()})})
    result = _get_at_path(schema, "data.target_skill")
    assert result == _str_schema()


def test_plain_object_schema_missing_path_raises():
    """Tier 2: _get_at_path raises on a missing segment in a plain object schema.

    Regression guard: error behaviour for ordinary schemas must be preserved.
    """
    schema = _obj({"data": _obj({"other": _str_schema()})})
    with pytest.raises(PreprocessorTypeError):
        _get_at_path(schema, "data.missing")


def test_plain_object_deep_path_resolves():
    """Tier 2: _get_at_path resolves a three-level deep path without any union keywords."""
    schema = _obj({"a": _obj({"b": _obj({"c": _str_schema()})})})
    result = _get_at_path(schema, "a.b.c")
    assert result == _str_schema()


# ── (h) eval_builder real schema ──────────────────────────────────────────────


def _make_eval_builder_union_schema() -> dict:
    """Construct the anyOf schema that eval_builder's analyze_skill phase sees.

    Mirrors what _union_schema() + artifact_to_json_schema() produce for
    input: user_message | eval_builder_request.
    """
    user_message_schema = {
        "type": "object",
        "properties": {
            "type": {"type": "string", "const": "user_message"},
            "data": {
                "type": "object",
                "description": "Natural language input from the user.",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "The raw user input text.",
                    }
                },
                "required": ["text"],
            },
        },
        "required": ["type", "data"],
    }
    eval_builder_request_schema = {
        "type": "object",
        "properties": {
            "type": {"type": "string", "const": "eval_builder_request"},
            "data": {
                "type": "object",
                "properties": {
                    "target_skill": {
                        "type": "string",
                        "description": "Short skill name only.",
                    }
                },
                "required": ["target_skill"],
            },
        },
        "required": ["type", "data"],
    }
    return {"anyOf": [user_message_schema, eval_builder_request_schema]}


def test_eval_builder_union_parent_path_data_accepted():
    """Tier 2: _require_parent_exists accepts 'data' as parent of 'data._prep' in union schema.

    This is the exact scenario that caused the B7-S5b compile-time failure.
    analyze_skill has `into: data._prep`; the input schema is the anyOf union
    of user_message and eval_builder_request. After the fix, parent path 'data'
    must be found in at least one branch and no PreprocessorTypeError is raised.
    """
    union_schema = _make_eval_builder_union_schema()
    # Must not raise — this is the core regression test
    _require_parent_exists(union_schema, "data._prep", "preprocessor step[0] (type='python')")


def test_eval_builder_union_data_target_skill_resolves():
    """Tier 2: _get_at_path resolves 'data.target_skill' in the eval_builder union schema.

    Verifies that the eval_builder_request branch's data.target_skill field is
    reachable through the union schema — confirming full path traversal works for
    the real schema shape.
    """
    union_schema = _make_eval_builder_union_schema()
    result = _get_at_path(union_schema, "data.target_skill")
    assert result.get("type") == "string"


def test_eval_builder_union_data_text_resolves():
    """Tier 2: _get_at_path resolves 'data.text' from the user_message branch.

    The user_message branch contributes 'data.text'; this must be reachable
    even though eval_builder_request does not have it.
    """
    union_schema = _make_eval_builder_union_schema()
    result = _get_at_path(union_schema, "data.text")
    assert result.get("type") == "string"
