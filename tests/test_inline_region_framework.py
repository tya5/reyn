"""Tier 2: the inline region framework (Region) — host/focus/navigate/select.

The region hosts RegionElements, tracks one focus cursor across their flattened
rows, and dispatches select to the owning element. An empty region collapses
(not visible) so an unused region is inert. Pinned with a recording fake element
(a real instance, no mocks).
"""
from __future__ import annotations

from reyn.interfaces.inline.region import DetailElement, Region


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


def test_detail_element_is_live_and_non_selectable() -> None:
    """Tier 2: F5 — DetailElement re-reads its provider on every lines() call (so
    a status panel stays live) and is non-selectable with an inert on_select."""
    box = {"rows": ["cost $0.01", "total 105"]}
    el = DetailElement(lambda: box["rows"])
    assert el.selectable is False
    assert el.lines() == ["cost $0.01", "total 105"]
    el.on_select(0)  # inert — no error, no state
    box["rows"] = ["cost $0.02"]
    assert el.lines() == ["cost $0.02"]  # live


def test_navigate_skips_non_selectable_rows() -> None:
    """Tier 2: F5 — the cursor steps over read-only (DetailElement) rows and never
    lands on one; a read-only-only region cannot be navigated."""
    ro = Region()
    ro.register(DetailElement(lambda: ["info-a", "info-b"]))
    assert ro.cursor_on_selectable is False
    ro.navigate(1)
    assert ro.cursor_on_selectable is False  # no selectable target to move to

    mixed = Region()
    mixed.register(DetailElement(lambda: ["detail-0"]))   # flat row 0 (read-only)
    mixed.register(_Element(["pick-0", "pick-1"]))        # flat rows 1, 2 (selectable)
    # the cursor resets to the first SELECTABLE row, skipping the detail row
    assert mixed.cursor == 1
    assert mixed.cursor_on_selectable is True
    mixed.navigate(1)
    assert mixed.cursor == 2
    mixed.navigate(-5)            # clamps at the first selectable row, not row 0
    assert mixed.cursor == 1
    assert mixed.at_first_selectable is True


def test_scroll_follows_cursor_down() -> None:
    """Tier 2: when set_max_visible(N) is set and the cursor moves below the visible
    window, scroll advances so the cursor stays in the visible range [scroll, scroll+N)."""
    region = Region()
    region.set_max_visible(3)
    region.register(_Element(["a", "b", "c", "d", "e"]))
    # cursor starts at 0, scroll = 0 → visible rows 0..2
    assert region.cursor == 0
    assert region.scroll == 0
    region.navigate(1)   # cursor → 1, still in [0..2]
    assert region.scroll == 0
    region.navigate(1)   # cursor → 2, still in [0..2]
    assert region.scroll == 0
    region.navigate(1)   # cursor → 3, exceeds scroll+3=3 → scroll → 1
    assert region.cursor == 3
    assert region.scroll == 1
    region.navigate(1)   # cursor → 4, scroll → 2
    assert region.cursor == 4
    assert region.scroll == 2


def test_scroll_follows_cursor_up() -> None:
    """Tier 2: when the cursor moves above the visible window, scroll retracts."""
    region = Region()
    region.set_max_visible(3)
    region.register(_Element(["a", "b", "c", "d", "e"]))
    region.navigate(4)   # jump to last row, scroll follows
    assert region.cursor == 4
    region.navigate(-3)  # cursor → 1; scroll retracts to keep cursor visible
    assert region.cursor == 1
    assert region.scroll <= 1


def test_scroll_resets_on_register() -> None:
    """Tier 2: registering a new element resets scroll to 0 (cursor back to top)."""
    region = Region()
    region.set_max_visible(3)
    region.register(_Element(["a", "b", "c", "d", "e"]))
    region.navigate(4)
    assert region.scroll > 0
    region.clear()
    assert region.scroll == 0
    region.register(_Element(["x", "y"]))
    assert region.scroll == 0


def test_select_inert_until_a_selectable_row_exists() -> None:
    """Tier 2: F5 — select on a read-only-only region is a no-op; once a
    selectable element is added the cursor reaches it and select fires."""
    el = _Element(["x"])
    region = Region()
    region.register(DetailElement(lambda: ["d0"]))
    region.select()              # only a detail row → nothing activated
    assert el.selected == []
    region.register(el)          # cursor resets onto the selectable row
    region.select()
    assert el.selected == [0]
