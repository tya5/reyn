"""Command-UI consumers of the inline region framework (F4).

A command-UI is a slash-command-driven selector hosted in the region — distinct
from an intervention (which is skill-driven and blocks a turn). The /rewind
checkpoint picker is the first; the generic ``CommandUIElement`` (display rows +
a submit-text per row) is intentionally command-agnostic so future command UIs
(and the status bar in F5) can host in the region without bespoke wiring.

On select, the element submits a plain slash command (e.g. ``/rewind 42``) through
the normal input path — so the action reuses the existing slash handler, and the
region only owns the selection UI.
"""
from __future__ import annotations

from typing import Callable


class CommandUIElement:
    """A RegionElement for a slash-command selector.

    ``lines()`` are the display rows; ``on_select(row)`` fires ``on_submit`` with
    the slash command to run for that row (``submit_texts[row]``).
    """

    def __init__(
        self,
        rows: list[str],
        submit_texts: list[str],
        on_submit: Callable[[str], None],
    ) -> None:
        self._rows = list(rows)
        self._submit_texts = list(submit_texts)
        self._on_submit = on_submit

    def lines(self) -> list[str]:
        return list(self._rows)

    def on_select(self, row: int) -> None:
        if 0 <= row < len(self._submit_texts):
            self._on_submit(self._submit_texts[row])


def rewind_rows(points: list[dict]) -> tuple[list[str], list[str]]:
    """Pure: (display rows, submit-texts) for the /rewind picker.

    ``points`` come from ``AgentRegistry.list_rewind_points()`` —
    ``[{"seq", "ts", "kind", "anchor", ...}, ...]``. Each row shows the seq + kind
    (+ anchor when present); selecting it submits ``/rewind <seq>``.
    """
    rows: list[str] = []
    submit_texts: list[str] = []
    for p in points:
        seq = p.get("seq")
        kind = p.get("kind", "?")
        anchor = p.get("anchor")
        label = f"seq {seq} · {kind}"
        if anchor:
            label += f" ({anchor})"
        rows.append(label)
        submit_texts.append(f"/rewind {seq}")
    return rows, submit_texts


def build_rewind_command_ui(
    points: list[dict], on_submit: Callable[[str], None]
) -> CommandUIElement:
    """Build the /rewind picker element from list_rewind_points() rows."""
    rows, submit_texts = rewind_rows(points)
    return CommandUIElement(rows, submit_texts, on_submit)
