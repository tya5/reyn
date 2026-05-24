"""Tier 2: ErrorBox carries a non-color (shape) channel for accessibility.

Visual UX audit (MED severity Finding F5): the only signal that a
mounted ErrorBox was an *error* was the ``#cc5555`` red text colour.
On a dark pane the contrast ratio sits around 3.5:1 — right at the
WCAG AA threshold for large text and below for normal text — so an
error scrolled past quickly or read by a color-blind user blends into
the surrounding ``dim`` greys.

Adding a vertical ``border-left`` gives a shape / position cue that
survives the failure modes the colour does not. This test pins the
contract that future CSS refactors cannot silently drop the bar.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from reyn.chat.tui.app import ReynTUIApp
from reyn.chat.tui.widgets import ConversationView
from reyn.chat.tui.widgets.error_box import ErrorBox


def _make_app() -> ReynTUIApp:
    return ReynTUIApp(
        registry=None,
        agent_name="test-agent",
        model="test-model",
        budget_tracker=None,
    )


@pytest.mark.asyncio
async def test_error_box_declares_left_border() -> None:
    """Tier 2b: An ErrorBox mounted in the conv pane has a coloured left border.

    Pinned at the computed-styles level so a refactor that moves the rule
    out of ``DEFAULT_CSS`` (and forgets to re-add it elsewhere) fails fast.
    """
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        conv.mount_error(
            message="something broke",
            details="line 1\nline 2",
            run_id_short="abcd",
            skill_name="test_skill",
        )
        await pilot.pause()

        box = conv.query_one(ErrorBox)
        border = box.styles.border_left
        # ``styles.border_left`` is a (style, Color) tuple. ``None`` /
        # ``("none", ...)`` would both indicate the bar is gone.
        assert border is not None, "border_left must be set"
        style, _color = border
        assert style and style != "none", (
            f"border_left style must be non-none (got {style!r})"
        )


@pytest.mark.asyncio
async def test_error_box_border_color_matches_header_red() -> None:
    """Tier 2b: The left bar uses the same hue family as the header text.

    Visual consistency check: the bar is the *non-color* channel, but
    when colour IS available it must agree with the header — otherwise
    the eye reads two competing red signals.
    """
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        conv.mount_error(message="boom")
        await pilot.pause()

        box = conv.query_one(ErrorBox)
        _style, color = box.styles.border_left
        # ``#cc5555`` = (204, 85, 85). Allow ±20 per channel so a future
        # palette nudge to a slightly different red doesn't break the test
        # while still catching e.g. an accidental switch to coral or grey.
        r, g, b = color.rgb
        assert r >= 180, f"border red channel weak: {color.rgb}"
        assert g <= 110, f"border green channel too high (not red?): {color.rgb}"
        assert b <= 110, f"border blue channel too high (not red?): {color.rgb}"


@pytest.mark.asyncio
async def test_error_box_left_bar_visible_in_render() -> None:
    """Tier 2b: The border actually renders — Textual reserves a column for it.

    Sanity check beyond the styles property: the widget's region must
    include the 1-cell border on its left edge. Without the border the
    widget's outer width would equal its content width; with the border
    it's at least content + 1.
    """
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        conv.mount_error(message="boom")
        await pilot.pause()

        box = conv.query_one(ErrorBox)
        # ``virtual_size`` / ``outer_size`` differ from ``content_size``
        # by the border + padding. Asserting outer ≥ content + 1 covers
        # the border-left without depending on Textual's exact API name.
        outer = box.outer_size
        content = box.content_size
        assert outer.width >= content.width + 1, (
            f"outer width must be ≥ content + 1 for border; "
            f"got outer={outer.width}, content={content.width}"
        )
