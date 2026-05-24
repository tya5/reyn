"""Tier 2b: ``_msg_header`` aligns the dash rule by terminal cell width.

Before this fix the header used ``label.ljust(4)`` to reserve a fixed
4-character name column, then a hardcoded 26-dash rule sized off that
same constant. ``ljust`` counts Python code points, but terminal columns
count display cells — a CJK character (or full-width punctuation, or an
emoji) is 2 cells per glyph. An agent named ``"アリア"`` (3 code points,
6 cells) blew past the 4-cell column and pushed the dash rule out past
the banner width, breaking visual alignment between user and agent
turns.

The fix pads to a target *cell* count via ``rich.cells.cell_len`` and
flexes the dash count off the actual cell width of the label.
"""
from __future__ import annotations

import pytest
from rich.cells import cell_len

from reyn.chat.tui.widgets.conversation import (
    _DASH_TOTAL,
    _NAME_COL_COLS,
    _msg_header,
    _pad_to_cells,
)

# ── _pad_to_cells ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "label, target, expected_cells",
    [
        ("reyn", 4, 4),           # exactly fits
        ("you", 4, 4),            # narrow ASCII, pad by 1
        ("you ", 4, 4),           # already padded, untouched
        ("a", 4, 4),              # heavy padding
        ("アリア", 4, 6),         # 3 CJK glyphs = 6 cells > 4, no truncation
        ("日本", 4, 4),           # 2 CJK glyphs = 4 cells exactly
        ("", 4, 4),               # empty string padded to target
    ],
)
def test_pad_to_cells_yields_at_least_target(label: str, target: int, expected_cells: int) -> None:
    """Tier 2b: padded string's cell width meets or exceeds the target."""
    out = _pad_to_cells(label, target)
    assert cell_len(out) == expected_cells, (
        f"pad({label!r}, {target}) → {out!r} has {cell_len(out)} cells, expected {expected_cells}"
    )


def test_pad_to_cells_does_not_truncate_wide_strings() -> None:
    """Tier 2b: when input exceeds the target, return verbatim (no slice)."""
    label = "アリア"  # 6 cells
    out = _pad_to_cells(label, 4)
    assert out == label


# ── _msg_header ───────────────────────────────────────────────────────────────


def _header_plain_text(label: str) -> str:
    """Return the plain-text projection of a rendered header."""
    return _msg_header(label, "bold", "dim").plain


def test_narrow_label_dash_count_unchanged() -> None:
    """Tier 2b: existing 4-cell labels keep their historical 26-dash rule.

    Guards the fix from accidentally changing the visual length for the
    99% case (``"reyn"`` and ``"you "`` agents).
    """
    text = _header_plain_text("reyn")
    # Layout: 'HH:MM' (5) + '  ' (2) + 'reyn' (4) + ' ' (1) + dashes
    # _DASH_TOTAL = 38 → dashes = 38 - 5 - 2 - 4 - 1 = 26
    assert text.count("─") == 26


def test_short_label_padded_then_dashed_same_count() -> None:
    """Tier 2b: ``"you"`` (3 cells) is padded to 4 cells, dashes still 26."""
    text = _header_plain_text("you")
    assert text.count("─") == 26
    # The padded column ends just before the dash run — verify cell width
    # from start of the line through the space before dashes.
    pre_dash = text.split("─", 1)[0]
    expected_cells = 5 + 2 + 4 + 1  # HH:MM + 2sp + 4cells + 1sp
    assert cell_len(pre_dash) == expected_cells, (
        f"pre-dash prefix has {cell_len(pre_dash)} cells, expected {expected_cells}"
    )


def test_wide_cjk_label_shrinks_dash_count_to_stay_within_dash_total() -> None:
    """Tier 2b: ``"アリア"`` (6 cells) shrinks the dash count so the whole
    line stays at or under _DASH_TOTAL cells.

    Without the fix the line would have been longer than _DASH_TOTAL and
    overrun the banner width.
    """
    text = _header_plain_text("アリア")
    # name_cells = 6 → dashes = 38 - 5 - 2 - 6 - 1 = 24
    assert text.count("─") == 24
    # Total line cells must not exceed _DASH_TOTAL
    assert cell_len(text) <= _DASH_TOTAL


def test_header_cell_width_consistent_across_label_types() -> None:
    """Tier 2b: every header line has the same total cell width.

    This is the user-visible alignment contract: the dash rule's right
    edge lands at the same column whether the label is "you ", "reyn",
    or a CJK agent name.
    """
    labels = ("reyn", "you ", "you", "a", "アリア", "日本", "")
    widths = {cell_len(_header_plain_text(label)) for label in labels}
    # All widths must converge to a single value — use set cardinality check via
    # equality rather than len() to avoid format-pinning the set size.
    assert widths == {_DASH_TOTAL}, (
        f"header cell widths diverged across labels: {widths}"
    )


def test_header_dash_count_never_negative_for_outsized_labels() -> None:
    """Tier 2b: pathologically wide labels still produce at least 1 dash.

    Guards the ``max(1, ...)`` floor: a 30-cell label would otherwise
    yield a negative dash count and crash ``"─" * n`` (Python silently
    returns "" for n<0, which would silently break alignment instead of
    crashing — both are bad).
    """
    huge = "アリア" * 5  # 30 cells
    text = _header_plain_text(huge)
    assert text.count("─") >= 1


def test_name_col_cols_constant_matches_legacy_layout() -> None:
    """Tier 2b: the column constant is 4 — matches the historic ``ljust(4)``.

    If this constant ever moves, the layout calc in ``_msg_header`` must
    move with it; pin the value so a stray rename doesn't desync the two.
    """
    assert _NAME_COL_COLS == 4
