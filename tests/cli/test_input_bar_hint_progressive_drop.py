"""Tier 2: InputBar footer hint degrades progressively at narrow widths (A4).

A4 (wave UX-exploration): the footer hint
  ``  Enter send │ Ctrl+J nl │ Ctrl+C cancel │ Ctrl+L clear │ Ctrl+B panel``
has ``height: 1`` CSS, which clips silently when the terminal is narrow.
At ~40 cols ``Ctrl+L``/``Ctrl+B`` disappear without warning; at ~55 cols
the tail starts to wrap to an invisible second line.

Fix: progressive field drop mirroring header.py's ``_choose_included_fields``
pattern.  Three tiers keyed on widget cell-width:

  ≥ 55 cols → full (all 5 keys)
  ≥ 40 cols → mid (Enter send │ Ctrl+J nl │ Ctrl+C cancel)
  <  40 cols → min (Enter │ Ctrl+C)

Public surface under test: ``InputBar._build_hint(width)`` return value
(= the plain string the Label receives).  No private state is read.
"""
from __future__ import annotations

import sys
from pathlib import Path

from rich.cells import cell_len

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def _hint(width: int = 0) -> str:
    from reyn.tui.widgets.input_bar import InputBar

    bar = InputBar.__new__(InputBar)
    return InputBar._build_hint(bar, width)


# ── width-threshold contracts ─────────────────────────────────────────────────


def test_hint_full_tier_at_80_cols() -> None:
    """Tier 2: at 80 cols the full hint is returned with all five keys."""
    hint = _hint(80)
    assert "Enter send" in hint
    assert "Ctrl+J nl" in hint
    assert "Ctrl+C cancel" in hint
    assert "Ctrl+L clear" in hint
    assert "Ctrl+B panel" in hint


def test_hint_full_tier_at_55_cols() -> None:
    """Tier 2: at exactly the full threshold (55 cols) the full hint is returned."""
    hint = _hint(55)
    assert "Ctrl+L clear" in hint
    assert "Ctrl+B panel" in hint


def test_hint_mid_tier_at_54_cols() -> None:
    """Tier 2: at 54 cols (one below full threshold) the mid hint is returned.

    Mid hint omits Ctrl+L clear and Ctrl+B panel (power-user / recovery keys)
    but preserves the multi-line affordance (Ctrl+J nl) and the essential
    cancel key (Ctrl+C cancel).
    """
    hint = _hint(54)
    assert "Enter send" in hint
    assert "Ctrl+J nl" in hint
    assert "Ctrl+C cancel" in hint
    # Dropped at this tier — no silent partial rendering.
    assert "Ctrl+L clear" not in hint
    assert "Ctrl+B panel" not in hint


def test_hint_mid_tier_at_40_cols() -> None:
    """Tier 2: at exactly the mid threshold (40 cols) the mid hint is returned."""
    hint = _hint(40)
    assert "Ctrl+C cancel" in hint
    assert "Ctrl+L clear" not in hint


def test_hint_min_tier_at_39_cols() -> None:
    """Tier 2: at 39 cols (one below mid threshold) the min hint is returned.

    Minimum hint keeps only Enter and Ctrl+C — the two load-bearing keys
    (submit + cancel) that must always be surfaced regardless of width.
    """
    hint = _hint(39)
    assert "Enter" in hint
    assert "Ctrl+C" in hint
    # Ctrl+J and Ctrl+L/B not present in min tier.
    assert "Ctrl+J" not in hint
    assert "Ctrl+L" not in hint


def test_hint_min_tier_at_20_cols() -> None:
    """Tier 2: at 20 cols the minimum hint is returned (not clipped full hint)."""
    hint = _hint(20)
    # Minimum hint must fit comfortably in 20 cols.
    width = cell_len(hint)
    assert width <= 20, (
        f"min hint is {width} cells, must fit ≤20 cols: {hint!r}"
    )
    assert "Enter" in hint
    assert "Ctrl+C" in hint


# ── zero-width (pre-mount) fallback ──────────────────────────────────────────


def test_hint_width_zero_returns_full_hint() -> None:
    """Tier 2: width=0 (pre-mount default) returns the full hint unchanged.

    Existing compose() calls _build_hint() with no width argument (= 0).
    The fallback must preserve the pre-A4 full hint so the widget still
    renders correctly on initial mount when size is not yet known.
    """
    hint = _hint(0)
    assert "Enter send" in hint
    assert "Ctrl+J nl" in hint
    assert "Ctrl+C cancel" in hint
    assert "Ctrl+L clear" in hint
    assert "Ctrl+B panel" in hint


# ── full hint cell budget unchanged ──────────────────────────────────────────


def test_full_hint_fits_72_cell_budget() -> None:
    """Tier 2: full hint (width=0 / ≥55) stays within the 72-cell footer budget.

    Default 80-col terminal minus 8 cells of conv-pane chrome leaves
    ~72 cells for the footer. Going over clips the trailing key.
    """
    hint = _hint(0)
    w = cell_len(hint)
    assert w <= 72, f"full hint is {w} cells, must be ≤72: {hint!r}"


def test_mid_hint_fits_55_cell_budget() -> None:
    """Tier 2: mid hint fits in the 55-col budget that triggers it."""
    hint = _hint(54)
    w = cell_len(hint)
    assert w <= 55, f"mid hint is {w} cells, must fit ≤55: {hint!r}"


def test_min_hint_fits_40_cell_budget() -> None:
    """Tier 2: min hint fits in the 40-col budget that triggers it."""
    hint = _hint(39)
    w = cell_len(hint)
    assert w <= 40, f"min hint is {w} cells, must fit ≤40: {hint!r}"


# ── separator consistency ─────────────────────────────────────────────────────


def test_full_hint_separator_count() -> None:
    """Tier 2: full hint uses │ as separator; 5 keys → 4 separators."""
    hint = _hint(0)
    assert hint.count("│") == 4, (
        f"expected 4 separators in full hint, got {hint.count('│')}: {hint!r}"
    )


def test_mid_hint_separator_count() -> None:
    """Tier 2: mid hint has 3 keys → 2 separators."""
    hint = _hint(54)
    assert hint.count("│") == 2, (
        f"expected 2 separators in mid hint, got {hint.count('│')}: {hint!r}"
    )
