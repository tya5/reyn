"""Tier 2: data/workspace/artifact_validator.py _resolve_path pure helper.

_resolve_path(context, path) traverses a nested dict using a dotted-path
expression and returns (members, ok). Supports plain key access ('a.b')
and wildcard array iteration ('a.items[*].name'). Returns ([], False) for
empty paths, missing keys, or wrong-type values.
"""
from __future__ import annotations

from reyn.data.workspace.artifact_validator import _resolve_path


def test_resolve_path_simple_nested_key() -> None:
    """Tier 2: 'a.b' resolves to context['a']['b'] wrapped in a list."""
    members, ok = _resolve_path({"a": {"b": "value"}}, "a.b")
    assert ok is True
    assert members == ["value"]


def test_resolve_path_single_key() -> None:
    """Tier 2: single-segment path returns the value at that key."""
    members, ok = _resolve_path({"x": 42}, "x")
    assert ok is True
    assert members == [42]


def test_resolve_path_list_value_at_leaf() -> None:
    """Tier 2: list value at the leaf is returned as the members list."""
    members, ok = _resolve_path({"a": [1, 2, 3]}, "a")
    assert ok is True
    assert members == [1, 2, 3]


def test_resolve_path_wildcard_collects_field_from_each_item() -> None:
    """Tier 2: 'items[*].name' collects the 'name' field from each array element."""
    ctx = {"items": [{"name": "alpha"}, {"name": "beta"}]}
    members, ok = _resolve_path(ctx, "items[*].name")
    assert ok is True
    assert members == ["alpha", "beta"]


def test_resolve_path_wildcard_nested_under_key() -> None:
    """Tier 2: wildcard can appear at any nesting level ('a.items[*].n')."""
    ctx = {"a": {"items": [{"n": "x"}, {"n": "y"}]}}
    members, ok = _resolve_path(ctx, "a.items[*].n")
    assert ok is True
    assert members == ["x", "y"]


def test_resolve_path_missing_key_returns_false() -> None:
    """Tier 2: path that traverses a missing key returns ([], False)."""
    members, ok = _resolve_path({"a": {"c": 1}}, "a.b")
    assert ok is False
    assert members == []


def test_resolve_path_non_dict_intermediate_returns_false() -> None:
    """Tier 2: scalar at a non-leaf position returns ([], False)."""
    members, ok = _resolve_path({"a": 42}, "a.b")
    assert ok is False
    assert members == []


def test_resolve_path_empty_path_returns_false() -> None:
    """Tier 2: empty path string returns ([], False)."""
    members, ok = _resolve_path({"a": 1}, "")
    assert ok is False
    assert members == []


def test_resolve_path_empty_context_missing_key_returns_false() -> None:
    """Tier 2: path into empty context returns ([], False)."""
    members, ok = _resolve_path({}, "a.b")
    assert ok is False
    assert members == []


def test_resolve_path_wildcard_on_non_list_returns_false() -> None:
    """Tier 2: '[*]' on a non-list value returns ([], False)."""
    members, ok = _resolve_path({"items": "not-a-list"}, "items[*].name")
    assert ok is False
    assert members == []
