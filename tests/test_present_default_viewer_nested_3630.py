"""Tier 2: the default viewer renders a nested object structurally, not as JSON.

`present`'s own description tells the model to omit `view` and `blueprint` — "a
sensible default view; you do not need to design anything" — so the default
viewer IS the path an operator gets without knowing present exists. It was not
sensible for the shape that path is written for: the description names "a query
result, a list", and tool results nest.

The dict branch read keys at depth 0 and emitted one `keyvalue` card. A
`keyvalue` row's `value` is a text slot, so a dict or list bound into one
renders as its JSON form. The list branch, meanwhile, descended a level to build
table columns. Identical data therefore rendered as a table when the top level
was a list and as a JSON blob when it was an object (#3630).

Asserted through the real synthesize -> validate -> resolve path on a registered
surface, so what is checked is the rendering the user would see, not the
blueprint the synthesizer happened to emit.
"""
from __future__ import annotations

from typing import Any

from reyn.core.present.binding import resolve_bindings
from reyn.core.present.catalog import validate_blueprint
from reyn.core.present.fallback import default_viewer_blueprint

# The owner's live payload, which is why the shape is a wrapper around a wrapper
# around the rows rather than something tidier.
_NESTED = {
    "code": 200,
    "message": "m",
    "data": {"items": [{"id": 1, "name": "A"}, {"id": 2, "name": "B"}]},
    "status": "success",
}


def _render(data: Any) -> Any:
    return resolve_bindings(
        validate_blueprint(default_viewer_blueprint(data)),
        data,
        surface="inline-cui",
        ref=None,
    )


def _components(rendered: Any) -> "list[str]":
    return [node.get("component") for node in rendered.nodes]


def test_a_nested_row_list_reaches_the_user_as_a_table() -> None:
    """Tier 2: the rows render as a table with their own columns."""
    rendered = _render(_NESTED)

    tables = [n for n in rendered.nodes if n.get("component") == "table"]
    assert tables, f"no table in the rendering: {_components(rendered)}"
    assert [c["header"] for c in tables[0]["columns"]] == ["id", "name"]
    # #3664: `rendered.rows` counts every rendered row across every rows-shaped
    # slot, not just the table's — the top-level keyvalue card (code/message/
    # status) contributes its own 3 rows alongside the table's 2.
    assert rendered.rows == 5


def test_nothing_is_flattened_to_json_on_the_way() -> None:
    """Tier 2: no value is coerced into a text slot.

    A container-into-text-slot coercion is recorded in `coerced` (#3664), so an
    empty `coerced` list is the direct statement that no container landed in a
    text slot — stronger than checking the table exists, which a rendering
    could satisfy while ALSO dumping something else.
    """
    rendered = _render(_NESTED)

    assert rendered.coerced == []
    assert rendered.bindings_dropped == []


def test_the_same_rows_render_the_same_either_way() -> None:
    """Tier 2: nesting no longer decides whether rows become a table.

    The asymmetry — table at the top level, JSON when wrapped — is the defect
    stated as a property, so this compares the two directly instead of pinning
    either one's output.
    """
    wrapped = _render({"items": [{"id": 1, "name": "A"}]})
    bare = _render([{"id": 1, "name": "A"}])

    assert "table" in _components(wrapped)
    assert "table" in _components(bare)
    assert wrapped.rows == bare.rows


def test_a_flat_object_still_renders_as_one_card() -> None:
    """Tier 2: the case that already worked is untouched.

    Descending must not fragment an object whose values are all scalars into a
    node per key — the single card was correct for it and stays.
    """
    rendered = _render({"code": 200, "status": "success"})

    assert _components(rendered) == ["keyvalue"]
    assert rendered.bindings_dropped == []


def test_descent_stops_and_says_so() -> None:
    """Tier 2: past the depth bound the value goes back to a text slot.

    Pinned because the bound is what keeps the synthesized view's size a
    property of the data's shape rather than its depth — an unbounded descent
    would emit a node per level. The coercion (#3664: displayed-but-reshaped,
    not a drop) is the honest signal that the floor was reached, so it is
    asserted rather than avoided.
    """
    deep: dict = {"a": {"b": {"c": {"d": {"e": {"f": 1}}}}}}

    rendered = _render(deep)

    assert [c["rendered_as"] for c in rendered.coerced] == ["json_text"]
    assert rendered.bindings_dropped == []
