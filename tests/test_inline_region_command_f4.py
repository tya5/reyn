"""Tier 2: command-UI region consumer (F4) — generic selector + /rewind picker.

A CommandUIElement is a region element whose rows each submit a slash command on
select; the /rewind picker is built from list_rewind_points(). Generic so future
command UIs (and F5's status bar) reuse it.
"""
from __future__ import annotations

from reyn.interfaces.inline.region_command import (
    CommandUIElement,
    build_rewind_command_ui,
    rewind_rows,
)


def test_rewind_rows_label_and_submit_per_point() -> None:
    """Tier 2: each rewind point → a 'seq N · kind' row + a '/rewind N' submit."""
    rows, submits = rewind_rows([
        {"seq": 42, "kind": "turn", "anchor": "a1"},
        {"seq": 38, "kind": "turn"},
    ])
    assert rows == ["seq 42 · turn (a1)", "seq 38 · turn"]
    assert submits == ["/rewind 42", "/rewind 38"]
    assert rewind_rows([]) == ([], [])


def test_command_ui_select_submits_the_rows_command() -> None:
    """Tier 2: selecting a row fires on_submit with that row's slash command."""
    submitted: list[str] = []
    el = CommandUIElement(
        ["a", "b"], ["/x 1", "/x 2"], submitted.append
    )
    assert el.lines() == ["a", "b"]
    el.on_select(1)
    assert submitted == ["/x 2"]
    el.on_select(99)  # out of range → no submit
    assert submitted == ["/x 2"]


def test_build_rewind_command_ui_select_submits_rewind_seq() -> None:
    """Tier 2: the built /rewind picker submits '/rewind <seq>' for the row."""
    submitted: list[str] = []
    el = build_rewind_command_ui(
        [{"seq": 42, "kind": "turn"}, {"seq": 38, "kind": "turn"}], submitted.append
    )
    assert el.lines() == ["seq 42 · turn", "seq 38 · turn"]
    el.on_select(0)
    assert submitted == ["/rewind 42"]
