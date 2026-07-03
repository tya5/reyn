"""Tier 2: command-UI region consumer (F4) — generic selector + /rewind picker.

A CommandUIElement is a region element whose rows each submit a slash command on
select; the /rewind picker is built from list_rewind_points(). Generic so future
command UIs (and F5's status bar) reuse it.
"""
from __future__ import annotations

from reyn.interfaces.inline.region_command import (
    _ANCHOR_MAX_LEN,
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


def test_rewind_rows_truncates_long_anchors() -> None:
    """Tier 2: anchors longer than _ANCHOR_MAX_LEN are truncated to max-1 chars + '…'
    so the picker rows stay readable in a typical terminal width."""
    long_anchor = "x" * (_ANCHOR_MAX_LEN + 20)
    rows, _ = rewind_rows([{"seq": 1, "kind": "turn", "anchor": long_anchor}])
    label = rows[0]
    # The anchor in parens should be at most _ANCHOR_MAX_LEN chars + parens.
    # Extract the anchor portion from "seq 1 · turn (<anchor>)".
    inner = label[label.index("(") + 1 : label.rindex(")")]
    assert len(inner) == _ANCHOR_MAX_LEN, f"expected {_ANCHOR_MAX_LEN} chars, got {len(inner)}"
    assert inner.endswith("…"), "truncated anchor must end with ellipsis"


def test_rewind_rows_short_anchor_is_unchanged() -> None:
    """Tier 2: an anchor at or under _ANCHOR_MAX_LEN is passed through verbatim."""
    short = "a" * _ANCHOR_MAX_LEN
    rows, _ = rewind_rows([{"seq": 1, "kind": "turn", "anchor": short}])
    assert f"({short})" in rows[0]


def test_rewind_rows_preserves_caller_order() -> None:
    """Tier 2: rewind_rows() is order-preserving — it shows rows in exactly the
    order the caller provides. The /rewind slash handler is responsible for passing
    list_rewind_points() in reverse (most-recent first) before calling this."""
    rows, submits = rewind_rows([
        {"seq": 100, "kind": "phase"},
        {"seq": 50, "kind": "turn"},
        {"seq": 10, "kind": "turn"},
    ])
    # Order unchanged — most-recent-first is the caller's responsibility.
    assert rows[0].startswith("seq 100"), f"expected seq 100 first, got: {rows}"
    assert rows[-1].startswith("seq 10"), f"expected seq 10 last, got: {rows}"
    assert submits == ["/rewind 100", "/rewind 50", "/rewind 10"]
