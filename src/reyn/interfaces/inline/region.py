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

from typing import Callable, Protocol, runtime_checkable


@runtime_checkable
class RegionElement(Protocol):
    """One hosted UI element — a list of selectable rows + a select action.

    ``lines()`` returns the element's current display rows (empty = nothing to
    show right now). ``on_select(row)`` is invoked with the 0-based row index
    within THIS element when the user activates the focused row.

    An element may expose ``selectable`` (default True if absent): False marks a
    read-only element whose rows carry no focus cursor and whose ``on_select`` is
    inert — see :class:`DetailElement`. Consumers draw the cursor only on
    selectable elements.
    """

    def lines(self) -> list[str]: ...

    def on_select(self, row: int) -> None: ...


class DetailElement:
    """A read-only RegionElement: live display rows, no selection.

    ``lines_provider`` is called on every render, so the rows stay live (e.g. a
    status detail panel reflecting current cost / token counts) without the
    element being rebuilt. ``selectable=False`` tells the renderer to draw no
    focus cursor and makes ``on_select`` a no-op — the panel is information, not
    a picker.
    """

    selectable = False

    def __init__(self, lines_provider: Callable[[], list[str]]) -> None:
        self._lines_provider = lines_provider

    def lines(self) -> list[str]:
        return list(self._lines_provider())

    def on_select(self, row: int) -> None:
        return None


class Region:
    """A focus-tracking stack of :class:`RegionElement`s."""

    def __init__(self) -> None:
        self._elements: list[RegionElement] = []
        self._cursor = 0

    def register(self, element: RegionElement) -> None:
        """Add an element and reset the focus cursor to the first selectable row."""
        self._elements.append(element)
        self._reset_cursor()

    def unregister(self, element: RegionElement) -> None:
        """Remove an element (idempotent) and reset the cursor."""
        if element in self._elements:
            self._elements.remove(element)
        self._reset_cursor()

    def clear(self) -> None:
        """Drop all elements (e.g. on teardown) and reset the cursor."""
        self._elements.clear()
        self._cursor = 0

    @staticmethod
    def _selectable(element: RegionElement) -> bool:
        """A read-only element (``selectable=False``) carries no focus cursor."""
        return getattr(element, "selectable", True)

    def _flat(self) -> list[tuple[RegionElement, int, str]]:
        """Flatten to ``(element, local_row, text)`` across all elements."""
        out: list[tuple[RegionElement, int, str]] = []
        for element in self._elements:
            for i, text in enumerate(element.lines()):
                out.append((element, i, text))
        return out

    def _selectable_indices(self) -> list[int]:
        """Flattened-row indices that belong to a selectable element."""
        return [i for i, (el, _, _) in enumerate(self._flat()) if self._selectable(el)]

    def _reset_cursor(self) -> None:
        sel = self._selectable_indices()
        self._cursor = sel[0] if sel else 0

    @property
    def visible(self) -> bool:
        """True when any element has rows to show — else the region collapses."""
        return any(element.lines() for element in self._elements)

    @property
    def cursor(self) -> int:
        """The focus cursor index across the flattened rows."""
        return self._cursor

    @property
    def at_first_selectable(self) -> bool:
        """True when the cursor sits on the first selectable row (or none exist).

        Lets a consumer treat ``↑`` at the top of a picker as "close" rather than
        a no-op move, matching the pre-region status menu.
        """
        sel = self._selectable_indices()
        return (not sel) or self._cursor == sel[0]

    @property
    def cursor_on_selectable(self) -> bool:
        """True when the cursor sits on a selectable row (draw the highlight)."""
        flat = self._flat()
        if 0 <= self._cursor < len(flat):
            return self._selectable(flat[self._cursor][0])
        return False

    def lines(self) -> list[str]:
        """All display rows across the hosted elements, in registration order."""
        return [text for _, _, text in self._flat()]

    def navigate(self, delta: int) -> None:
        """Move the cursor ``delta`` SELECTABLE rows, skipping read-only ones.

        Magnitude is honoured (``delta=3`` advances three selectable rows) and
        clamped to the selectable range; read-only (``DetailElement``) rows are
        stepped over, never landed on. With no selectable rows the cursor is
        inert — a read-only-only region cannot be navigated.
        """
        sel = self._selectable_indices()
        if not sel:
            return
        if self._cursor in sel:
            pos = sel.index(self._cursor)
        else:
            # cursor is on a read-only row: snap to the first selectable at/after
            # it (or the last selectable before it) so delta moves from there.
            after = [k for k, i in enumerate(sel) if i >= self._cursor]
            pos = after[0] if after else len(sel) - 1
        pos = max(0, min(pos + delta, len(sel) - 1))
        self._cursor = sel[pos]

    def select(self) -> None:
        """Activate the focused row → its owning element's ``on_select``.

        Inert on a read-only row (a non-selectable element is never activated).
        """
        flat = self._flat()
        if 0 <= self._cursor < len(flat):
            element, local_row, _ = flat[self._cursor]
            if self._selectable(element):
                element.on_select(local_row)
