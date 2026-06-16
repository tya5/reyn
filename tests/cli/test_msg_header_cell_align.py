"""Tier 2: ``_msg_header`` symbol-only layout (ts-on and ts-off modes).

Before the Claude Code-style refactor, the header used a padded label
(``▶ you``, ``◆ reyn``) + a dash rule sized off the label cell-width.
The new header is symbol-only: either ``HH:MM <symbol>`` (ts on) or
``<symbol>`` (ts off). No dash rule; no label text.

``_pad_to_cells`` is still in the module (used elsewhere) so its tests
are preserved.
"""
from __future__ import annotations

import re

import pytest
from rich.cells import cell_len

from reyn.tui.widgets.conversation import (
    _GLYPH_AGENT,
    _GLYPH_SYSTEM,
    _GLYPH_USER,
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


# ── _msg_header (new symbol-only layout) ─────────────────────────────────────


def test_msg_header_ts_on_contains_timestamp():
    """Tier 2: ts-on header contains an HH:MM pattern."""
    hdr = _msg_header(_GLYPH_USER, "bold #4abbb5", show_ts=True)
    plain = hdr.plain
    assert re.search(r"\d{2}:\d{2}", plain), (
        f"ts-on header must contain HH:MM, got: {plain!r}"
    )
    assert _GLYPH_USER in plain


def test_msg_header_ts_on_has_symbol():
    """Tier 2: ts-on header contains the expected symbol for each speaker."""
    for sym in (_GLYPH_USER, _GLYPH_AGENT, _GLYPH_SYSTEM):
        hdr = _msg_header(sym, "bold", show_ts=True)
        assert sym in hdr.plain, (
            f"ts-on header must contain symbol {sym!r}: {hdr.plain!r}"
        )


def test_msg_header_ts_off_has_no_timestamp():
    """Tier 2: ts-off header does NOT contain an HH:MM pattern."""
    hdr = _msg_header(_GLYPH_USER, "bold #4abbb5", show_ts=False)
    plain = hdr.plain
    assert not re.search(r"\d{2}:\d{2}", plain), (
        f"ts-off header must NOT contain HH:MM, got: {plain!r}"
    )
    assert _GLYPH_USER in plain


def test_msg_header_ts_off_starts_with_symbol():
    """Tier 2: ts-off header starts with the symbol at col 0 (no leading space)."""
    hdr = _msg_header(_GLYPH_USER, "bold #4abbb5", show_ts=False)
    assert hdr.plain.startswith(_GLYPH_USER), (
        f"ts-off header must start with symbol; got {hdr.plain!r}"
    )


def test_msg_header_no_dash_rule():
    """Tier 2: the new symbol-only header does NOT emit dash characters.

    The old layout used ``─────`` to separate turns. The new layout uses
    blank lines between turns instead, so no dash characters should appear
    in the header.
    """
    for sym in (_GLYPH_USER, _GLYPH_AGENT, _GLYPH_SYSTEM):
        for show_ts in (True, False):
            hdr = _msg_header(sym, "bold", show_ts=show_ts)
            assert "─" not in hdr.plain, (
                f"header must not contain dash rule: {hdr.plain!r} "
                f"(sym={sym!r}, show_ts={show_ts})"
            )
