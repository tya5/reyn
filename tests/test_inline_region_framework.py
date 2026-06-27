"""Tier 2: the inline region framework (Region) — host/focus/navigate/select.

The region hosts RegionElements, tracks one focus cursor across their flattened
rows, and dispatches select to the owning element. An empty region collapses
(not visible) so an unused region is inert. Pinned with a recording fake element
(a real instance, no mocks).
"""
from __future__ import annotations

from reyn.interfaces.inline.region import Region


class _Element:
    """A real RegionElement double: fixed rows, records on_select calls."""

    def __init__(self, rows: list[str]) -> None:
        self._rows = rows
        self.selected: list[int] = []

    def lines(self) -> list[str]:
        return list(self._rows)

    def on_select(self, row: int) -> None:
        self.selected.append(row)


def test_empty_region_is_inert() -> None:
    """Tier 2: with no elements the region is invisible and navigate/select no-op."""
    region = Region()
    assert region.visible is False
    assert region.lines() == []
    region.navigate(1)  # no rows → cursor stays
    region.select()     # nothing to activate
    assert region.cursor == 0


def test_register_makes_region_visible_with_rows() -> None:
    """Tier 2: a registered element with rows makes the region visible."""
    region = Region()
    region.register(_Element(["a", "b"]))
    assert region.visible is True
    assert region.lines() == ["a", "b"]


def test_navigate_clamps_within_flattened_rows() -> None:
    """Tier 2: the cursor moves across rows and clamps at both ends."""
    region = Region()
    region.register(_Element(["a", "b", "c"]))
    region.navigate(1)
    assert region.cursor == 1
    region.navigate(5)            # clamps at the last row
    assert region.cursor == 2
    region.navigate(-10)          # clamps at the first row
    assert region.cursor == 0


def test_select_dispatches_to_owning_element_local_row() -> None:
    """Tier 2: select activates the focused row on the right element, with the
    element-local row index (cursor spans multiple elements)."""
    e1 = _Element(["x0", "x1"])
    e2 = _Element(["y0", "y1"])
    region = Region()
    region.register(e1)
    region.register(e2)
    region.navigate(3)            # cursor at flattened row 3 → e2's local row 1
    region.select()
    assert e2.selected == [1]
    assert e1.selected == []


def test_unregister_and_clear_collapse_the_region() -> None:
    """Tier 2: removing elements makes the region invisible again."""
    e = _Element(["a"])
    region = Region()
    region.register(e)
    region.unregister(e)
    assert region.visible is False
    region.register(_Element(["b"]))
    region.clear()
    assert region.visible is False
    assert region.lines() == []
