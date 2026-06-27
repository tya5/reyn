"""Generic stacked-element region for the inline CUI (above / below the input).

A ``Region`` hosts UI elements that register themselves; it tracks a single focus
cursor across the elements' flattened selectable rows and dispatches navigate /
select to the owning element. An empty region is not visible, so an unused region
collapses and is inert — the framework adds no behaviour until a consumer
registers an element.

This is the framework skeleton (PR-F1): the above-input region (interventions,
command UIs like the /rewind selector) and the below-input region (the status
menu) are both meant to be ``Region`` instances. Consumers (typed-input
interventions etc.) arrive in later slices; until then a Region has zero elements
and changes nothing.

Closed-set inputs (select / confirm / grant-deny) are row-selectors and fit the
``RegionElement`` protocol below. Free-text input keeps using the normal input
field, so it needs no region element.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class RegionElement(Protocol):
    """One hosted UI element — a list of selectable rows + a select action.

    ``lines()`` returns the element's current display rows (empty = nothing to
    show right now). ``on_select(row)`` is invoked with the 0-based row index
    within THIS element when the user activates the focused row.
    """

    def lines(self) -> list[str]: ...

    def on_select(self, row: int) -> None: ...


class Region:
    """A focus-tracking stack of :class:`RegionElement`s."""

    def __init__(self) -> None:
        self._elements: list[RegionElement] = []
        self._cursor = 0

    def register(self, element: RegionElement) -> None:
        """Add an element and reset the focus cursor to the top."""
        self._elements.append(element)
        self._cursor = 0

    def unregister(self, element: RegionElement) -> None:
        """Remove an element (idempotent) and reset the cursor."""
        if element in self._elements:
            self._elements.remove(element)
        self._cursor = 0

    def clear(self) -> None:
        """Drop all elements (e.g. on teardown) and reset the cursor."""
        self._elements.clear()
        self._cursor = 0

    def _flat(self) -> list[tuple[RegionElement, int, str]]:
        """Flatten to ``(element, local_row, text)`` across all elements."""
        out: list[tuple[RegionElement, int, str]] = []
        for element in self._elements:
            for i, text in enumerate(element.lines()):
                out.append((element, i, text))
        return out

    @property
    def visible(self) -> bool:
        """True when any element has rows to show — else the region collapses."""
        return any(element.lines() for element in self._elements)

    @property
    def cursor(self) -> int:
        """The focus cursor index across the flattened rows."""
        return self._cursor

    def lines(self) -> list[str]:
        """All display rows across the hosted elements, in registration order."""
        return [text for _, _, text in self._flat()]

    def navigate(self, delta: int) -> None:
        """Move the focus cursor by ``delta`` rows, clamped to the row range."""
        n = len(self._flat())
        if n:
            self._cursor = max(0, min(self._cursor + delta, n - 1))

    def select(self) -> None:
        """Activate the focused row → its owning element's ``on_select``."""
        flat = self._flat()
        if 0 <= self._cursor < len(flat):
            element, local_row, _ = flat[self._cursor]
            element.on_select(local_row)
