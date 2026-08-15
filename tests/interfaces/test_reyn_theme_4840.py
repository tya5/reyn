"""Tier 2: reyn's own default Textual theme resolves to concrete colours,
never an ``ansi_*`` marker, and honours #3542's "quiet, not loud" selection
band under its own values.

#4840, owner ruling (2026-08-16): reyn's default is now a full-colour theme
it owns, not a deferral to the terminal's ``ansi-*`` family. What this Tier
2 (OS invariant) pins is the CONSEQUENCE of that ruling holding: every
design-system variable reyn's own theme produces is a concrete colour — the
same property #4850's crash needed absent (Textual's ``ANSIToTruecolor``
filter crashes unpacking a ``None`` ``.triplet``, which only an
``ansi_*``-marker colour has) — not the specific RGB picked (a visual-taste
call this test does not gate; CLAUDE.md's colour policy is about MEANING).

Time-dependency check: 0 instances.
"""
from __future__ import annotations

from textual.color import Color

from reyn.interfaces.inline.textual_chat.theme import REYN_THEME


def _resolved(name: str) -> Color:
    colors = REYN_THEME.to_color_system().generate()
    return Color.parse(colors[name])


def test_the_theme_is_not_ansi() -> None:
    """Tier 2: reyn's own theme resolves colours itself — it does not defer
    to the terminal (`ansi=True` disables Textual's truecolor-conversion
    filter entirely, the property every ``ansi-*`` theme relies on)."""
    assert REYN_THEME.ansi is False


def test_every_base_design_variable_is_concrete() -> None:
    """Tier 2: none of the theme's core roles resolve to a ``.triplet``-less
    marker colour — the precondition #4850's crash needed (measured:
    ``Color.parse("ansi_default").rich_color.triplet is None``)."""
    for name in (
        "background",
        "surface",
        "panel",
        "foreground",
        "primary",
        "secondary",
        "warning",
        "error",
        "success",
        "accent",
    ):
        resolved = _resolved(name)
        assert resolved.rich_color.triplet is not None, (
            f"{name} resolved to a marker colour with no RGB triplet"
        )


def test_the_selection_band_is_concrete_and_distinct_from_the_accent() -> None:
    """Tier 2: #3542 read explicitly (per #4840's own ruling) — the
    selection band must be a muted, DISTINCT hue, not the full-intensity
    accent Textual's own auto-derivation (`primary.with_alpha(0.5)`) would
    produce, and not a marker colour either."""
    selection_bg = _resolved("screen-selection-background")
    primary = _resolved("primary")
    assert selection_bg.rich_color.triplet is not None
    assert selection_bg.hex != primary.hex


def test_the_selection_band_meets_wcag_aa_contrast() -> None:
    """Tier 2: #3542's own concern was legibility as much as loudness — the
    selection text must stay readable against its own background."""
    bg = _resolved("screen-selection-background")
    fg = _resolved("screen-selection-foreground")

    def relative_luminance(color: Color) -> float:
        def lin(c: float) -> float:
            return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

        r, g, b = lin(color.r / 255), lin(color.g / 255), lin(color.b / 255)
        return 0.2126 * r + 0.7152 * g + 0.0722 * b

    l1, l2 = relative_luminance(bg), relative_luminance(fg)
    l1, l2 = max(l1, l2), min(l1, l2)
    contrast = (l1 + 0.05) / (l2 + 0.05)
    assert contrast >= 4.5, f"selection band contrast {contrast:.2f} below WCAG AA"
