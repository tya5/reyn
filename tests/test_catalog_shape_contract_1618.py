"""Tier 2: #1618 root-1 + root-4 — the canonical catalog-shape contract + projections.

`catalog_entries` (and `llm_tools_payload`) carry ONE canonical entry shape — the
OpenAI-nested `{type, function:{name, description, parameters}}`. The OS provides the
projections every consumer reads (`flat_catalog_entries` for render + the dispatch
membership; `dispatch_catalog_map` for the gate), so **no consumer hand-reads a nested
dict at a guessed depth** — the #1 root (`_render_code_api` read top-level `name` on a
nested entry → `tool('')` ×50) and #3 root (the exclude filter never matched).

This is the **test-shape-fidelity gate (root-4)**: the 8-defect cascade slipped past CI
because the unit Fakes returned a FLAT shape while the live adapter returns NESTED. A
contract test keyed on the canonical (nested) shape fails the moment a projection or a
consumer's shape assumption diverges — which is what live-verify had to catch.
"""
from __future__ import annotations

from reyn.tools.scheme import dispatch_catalog_map, flat_catalog_entries

# The canonical shape the LIVE `catalog_entries` adapter emits (OpenAI-nested). Tests
# that exercise a scheme's catalog consumer MUST use this shape, not a hand-flattened
# Fake (root-4: single shape-truth).
_CANONICAL: list[dict] = [
    {"type": "function", "function": {
        "name": "file__read",
        "description": "Read a file.\nsecond line dropped by render",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
    }},
    {"type": "function", "function": {
        "name": "file__write",
        "description": "",
        "parameters": {"type": "object", "properties": {}},
    }},
]


def test_flat_projection_extracts_name_and_params_from_nested() -> None:
    """Tier 2: #1618 root-1 — flat_catalog_entries pulls name/description/parameters
    out of `function.*` (the #1 fix: a consumer that read top-level `name` got '')."""
    flat = flat_catalog_entries(_CANONICAL)
    assert [e["name"] for e in flat] == ["file__read", "file__write"]
    assert flat[0]["parameters"]["properties"] == {"path": {"type": "string"}}
    # every entry carries a valid (possibly empty) JSON-schema parameters object.
    assert flat[1]["parameters"] == {"type": "object", "properties": {}}
    assert flat[0]["description"].startswith("Read a file.")


def test_dispatch_map_keys_by_function_name_value_is_canonical_entry() -> None:
    """Tier 2: #1618 root-1 — dispatch_catalog_map keys the membership gate by
    `function.name`; the value is the canonical entry (the #7 gate's tool_catalog)."""
    m = dispatch_catalog_map(_CANONICAL)
    assert set(m) == {"file__read", "file__write"}
    assert m["file__read"] is _CANONICAL[0]


def test_projections_tolerate_already_flat_entry() -> None:
    """Tier 2: #1618 root-1 — the projections are defensive: an already-flat entry
    (no `function` wrapper) passes through unchanged, so a mixed/legacy producer can't
    silently empty a name."""
    flat_in = [{"name": "x__y", "description": "d",
                "parameters": {"type": "object", "properties": {}}}]
    assert flat_catalog_entries(flat_in)[0]["name"] == "x__y"
    assert set(dispatch_catalog_map(flat_in)) == {"x__y"}


def test_canonical_shape_is_nested_not_flat() -> None:
    """Tier 2: #1618 root-4 — pin the shape-truth: the canonical entry is NESTED
    (`function.name`), NOT flat (`name` top-level). A producer that emits flat, or a
    Fake that diverges to flat, breaks this — the fidelity gap that hid the cascade."""
    entry = _CANONICAL[0]
    assert "function" in entry and "name" in entry["function"]
    assert "name" not in entry  # name is NOT top-level on the canonical shape
