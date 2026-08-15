"""Tier 2: the drag-selection band asks for plain blue, not bright blue.

#3542, owner-adjudicated: the selection read as too loud against the
conversation. Textual's `ansi-dark` defaults `screen-selection-background` to
`ansi_bright_blue` (slot 12); reyn now asks for `ansi_blue` (slot 4).

Both are ANSI FRAMES, not colours. What blue actually looks like is the
terminal theme's decision, and it stays that way — reyn was never overriding
the operator's palette here, it only chooses which of the sixteen slots to
request. That distinction is what these tests pin: the assertions are on the
`ansi` slot number, not on RGB, because an RGB assertion would pass while
quietly meaning reyn had started dictating the colour.

`text-style: reverse` was considered and rejected. Textual COMPOSES the
selection style onto each cell, so reverse would let every coloured run — tool
rows, amber intervention headings, dim chrome — become its own background, and
the band would fragment. That answers a complaint nobody made; the complaint
was loudness, not uniformity.
"""
from __future__ import annotations

import pytest
from textual.app import App, ComposeResult
from textual.widgets import Static

from reyn.interfaces import palette

#: ANSI slot numbers, named so the assertions below read as intent rather than
#: as magic numbers. 4 is blue; 12 is its bright counterpart.
_ANSI_BLUE = 4
_ANSI_BRIGHT_BLUE = 12
_ANSI_BLACK = 0


class _Probe(App):
    CSS = palette.css(
        """
Screen > .screen--selection {
    background: @selection-bg@;
    color: @selection-fg@;
}
"""
    )

    def compose(self) -> ComposeResult:
        yield Static("x")


def test_the_tokens_name_ansi_frames_not_colours() -> None:
    """Tier 2: the palette asks for slots, so the terminal keeps deciding.

    A hex value here would look equivalent and would take the choice away from
    the operator's theme — the thing this change is careful NOT to do.
    """
    assert palette.TOKENS["@selection-bg@"] == "ansi_blue"
    assert palette.TOKENS["@selection-fg@"] == "ansi_black"


@pytest.mark.asyncio
async def test_the_selection_resolves_to_plain_blue_under_ansi_dark() -> None:
    """Tier 2: the band lands on slot 4, not Textual's default slot 12.

    Asserted on the resolved component style rather than on the stylesheet
    text: a rule can be present and lose to something later in the cascade,
    which is the failure mode this repo keeps finding.
    """
    app = _Probe()
    async with app.run_test(size=(40, 10)) as pilot:
        app.theme = "ansi-dark"
        await pilot.pause()

        styles = app.screen.get_component_styles("screen--selection")

        assert styles.background.ansi == _ANSI_BLUE
        assert styles.background.ansi != _ANSI_BRIGHT_BLUE


@pytest.mark.asyncio
async def test_the_text_inside_the_band_is_unchanged() -> None:
    """Tier 2: only the background moved.

    Pinned because changing a background without its foreground is how a
    readable pair turns into an unreadable one — and the fix for "too loud"
    must not become "now I cannot read it".
    """
    app = _Probe()
    async with app.run_test(size=(40, 10)) as pilot:
        app.theme = "ansi-dark"
        await pilot.pause()

        assert app.screen.get_component_styles("screen--selection").color.ansi == _ANSI_BLACK
