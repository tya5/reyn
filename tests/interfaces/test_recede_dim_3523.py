"""Tier 2: two pieces of interface text recede, without pinning a colour.

#3523, owner-adjudicated: the search bar's match count (`alpha 1/1`) reads as
two equal halves, so what the operator typed and what the interface answered
look alike; and `/rewind`'s heading competes with the rows it labels.

Both already asked for `$text-muted`, which under the `ansi-*` themes resolves
to the same `ansi_default` marker as ordinary text — measured on #3522/#3528.
They receded by nothing. `dim` is an SGR attribute: it changes what is drawn
and leaves the hue to the terminal, which is the owner's standing rule — adopt
the meaning the terminal already has rather than inventing one.

The assertions read the RESOLVED style rather than the stylesheet text, because
"the rule is present" and "the rule does anything" are the two things #3522
showed can differ.
"""
from __future__ import annotations

import pytest
from textual.app import App, ComposeResult

from reyn.interfaces.inline.textual_chat.rewind_picker import RewindPicker
from reyn.interfaces.inline.textual_chat.search_bar import SearchBar
from reyn.interfaces.palette import TOKENS


class _Probe(App):
    def compose(self) -> ComposeResult:
        yield SearchBar()
        yield RewindPicker()


def test_the_recede_token_is_an_attribute_not_a_colour() -> None:
    """Tier 2: the token pins no hue.

    A colour here would be the defect these two changes exist to avoid — under
    a themed terminal it either does nothing or overrides a choice that is the
    operator's to make.
    """
    assert TOKENS["@recede@"] == "dim"


@pytest.mark.asyncio
async def test_the_match_count_recedes_under_the_ansi_theme() -> None:
    """Tier 2: `1/1` is dimmed where `$text-muted` did nothing.

    Asserted under `ansi-dark` specifically: that is the theme where the old
    rule was inert, so a test under any other theme would pass without
    witnessing the fix.
    """
    app = _Probe()
    async with app.run_test() as pilot:
        app.theme = "ansi-dark"
        await pilot.pause()

        assert "dim" in str(app.query_one("#search-count").styles.text_style)


@pytest.mark.asyncio
async def test_the_rewind_heading_recedes_and_keeps_the_terminal_colours() -> None:
    """Tier 2: the heading dims, and no foreground or background is pinned.

    Checked on the rendered segment rather than on the style object — what
    reaches the terminal is the thing the owner sees, and a rule can resolve
    correctly while something downstream paints over it.
    """
    app = _Probe()
    async with app.run_test(size=(80, 20)) as pilot:
        app.theme = "ansi-dark"
        app.query_one(RewindPicker).display = True
        await pilot.pause()

        segment = next(iter(app.query_one("#rewind-picker-title").render_line(0)))
        style = str(segment.style)

        assert "dim" in style
        assert "default on default" in style, (
            f"the heading pinned a colour instead of dimming: {style}"
        )
