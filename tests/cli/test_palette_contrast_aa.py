"""Tier 2: accessibility — adjusted palette tokens meet WCAG AA on the panel bg.

These tokens label informational text (cancel/abort messages, cost figures,
the ErrorBox recovery hint) and were below WCAG AA 4.5:1 against the panel
background; they were lightened (staying in their own hue lane + matching the
warm worldview) to clear 4.5:1. This pins the contrast INVARIANT — the
accessibility guarantee — not the exact hex (any future on-brand value that
keeps ≥4.5:1 passes), so it is a behavioural assertion, NOT a format-pin.

Reference: WCAG 2.1 SC 1.4.3 — 4.5:1 for normal text. Dark-mode guidance also
favours muted/desaturated tones on a soft-black bg (these stay muted).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from reyn.chat.tui._palette import (
    _BG_PANEL,
    _HINT_ACTION,
    _RED_MUTED,
    _STATUS_SUCCESS_DARK,
)

_AA_NORMAL = 4.5


def _relative_luminance(hex_color: str) -> float:
    """WCAG relative luminance of an ``#rrggbb`` colour."""
    h = hex_color.lstrip("#")
    channels = [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)]

    def _linear(c: float) -> float:
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (_linear(c) for c in channels)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _contrast_ratio(fg: str, bg: str) -> float:
    """WCAG contrast ratio between two ``#rrggbb`` colours (≥ 1.0)."""
    l1, l2 = _relative_luminance(fg), _relative_luminance(bg)
    hi, lo = max(l1, l2), min(l1, l2)
    return (hi + 0.05) / (lo + 0.05)


@pytest.mark.parametrize(
    "name, token",
    [
        ("_RED_MUTED", _RED_MUTED),
        ("_STATUS_SUCCESS_DARK", _STATUS_SUCCESS_DARK),
        ("_HINT_ACTION", _HINT_ACTION),
    ],
)
def test_informational_token_meets_aa_on_panel_bg(name: str, token: str) -> None:
    """Tier 2: token carrying informational text clears WCAG AA on the panel bg."""
    ratio = _contrast_ratio(token, _BG_PANEL)
    assert ratio >= _AA_NORMAL, (
        f"{name} ({token}) contrast on {_BG_PANEL} is {ratio:.2f}:1, below WCAG "
        f"AA {_AA_NORMAL}:1. This token labels informational text and must stay "
        "legible — pick an on-brand value that keeps ≥4.5:1."
    )


def test_contrast_helper_sanity() -> None:
    """Tier 2: the contrast helper matches known WCAG anchors (guards the math)."""
    # Black vs white is the maximum 21:1; identical colours are 1:1.
    assert round(_contrast_ratio("#000000", "#ffffff")) == 21
    assert _contrast_ratio("#777777", "#777777") == pytest.approx(1.0)
