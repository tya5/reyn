"""Tier 2: eb-inline-hint defensive CSS (I-F12).

Wave-10 follow-up Topic I finding F12 (P3): the
``ErrorBox Label.eb-inline-hint`` CSS lacked ``display: none``
default while its sibling ``.eb-hint`` had it. Today the
``compose()`` ``if self._inline_hint:`` guard prevents an empty
Label from ever being mounted, so the asymmetric CSS doesn't
cause visible breakage. But if a future refactor drops or
breaks that guard, an empty Label would land in the DOM and
grab a layout row for nothing.

After the fix the inline-hint Label defaults to ``display: none``
and only becomes visible via a ``.-has-content`` modifier class
applied at yield time. The ``if`` guard remains the primary
gate; the class is defense-in-depth.

Public surfaces tested:
  - non-empty hint → Label mounted with both classes
  - empty hint → Label not mounted (regression — current
    behaviour unchanged)
  - CSS string contains both the ``display: none`` default and the
    ``.-has-content`` toggle (= the contract pair)
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


@pytest.mark.asyncio
async def test_inline_hint_label_carries_has_content_class_when_set() -> None:
    """Tier 2: non-empty inline_hint → Label tagged ``-has-content``."""
    from textual.widgets import Label

    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets import ConversationView

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        # Message with " • hint" trailer → triggers inline_hint extraction.
        box = conv.mount_error(message="something broke • retry in 30s")
        await pilot.pause()

        labels = list(box.query(Label))
        inline_hints = [
            lbl for lbl in labels if lbl.has_class("eb-inline-hint")
        ]
        assert inline_hints, "inline hint Label should be mounted"
        hint = inline_hints[0]
        assert hint.has_class("-has-content"), (
            f"inline hint should carry -has-content modifier; "
            f"classes: {hint.classes!r}"
        )


@pytest.mark.asyncio
async def test_empty_inline_hint_does_not_mount_label() -> None:
    """Tier 2b: no `• …` trailer → no inline hint Label (regression)."""
    from textual.widgets import Label

    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets import ConversationView

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        box = conv.mount_error(message="something broke")  # no • hint
        await pilot.pause()

        labels = list(box.query(Label))
        inline_hints = [
            lbl for lbl in labels if lbl.has_class("eb-inline-hint")
        ]
        assert inline_hints == [], (
            f"no hint trailer → no inline hint Label should mount; "
            f"got {len(inline_hints)} labels"
        )


def test_inline_hint_css_has_display_none_default_and_has_content_toggle() -> None:
    """Tier 2: CSS pair (default-hidden + visible-modifier) is in place."""
    from reyn.chat.tui.widgets.error_box import ErrorBox

    css = ErrorBox.DEFAULT_CSS
    # Default is hidden.
    assert ".eb-inline-hint {" in css
    eb_inline_block = css.split(".eb-inline-hint {", 1)[1].split("}", 1)[0]
    assert "display: none" in eb_inline_block, (
        f"eb-inline-hint should have display:none default; got "
        f"block:\n{eb_inline_block!r}"
    )
    # And the visibility-toggle modifier exists.
    assert ".eb-inline-hint.-has-content" in css, (
        f"missing .-has-content modifier in CSS; got:\n{css!r}"
    )
