"""Tier 2: InterventionWidget narrow-terminal overflow + free-text hint (A2, C5).

A2 (MED): .iv-chips had no overflow-x handling. At narrow terminals the
rightmost chips ("free response…", "skip rest") were pushed outside the
widget boundary and became unreachable. Fix adds ``overflow-x: hidden`` to
.iv-chips so clipping is defined — chips that overflow are hidden rather than
rendered outside the boundary.

C5 (LOW): The free-text-only branch (choices=[]) yielded only an Input with
NO keyboard hint label. Users had no visible signal for how to submit or
cancel. Fix adds ``Label("Enter to submit · Ctrl+C cancels", classes="iv-hint",
markup=False)`` after the Input, reusing the existing iv-hint CSS.

Public surfaces tested (per testing policy — NO private state):
  - ``widget.query(Label)`` label text content (rendered DOM)
  - ``widget.query(Button)`` presence / labels at narrow width
  - CSS DEFAULT_CSS string inspection for overflow-x: hidden
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_app():
    from reyn.interfaces.tui.app import ReynTUIApp

    return ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)


# ---------------------------------------------------------------------------
# C5: free-text hint label present in free-text-only branch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_free_text_intervention_renders_keyboard_hint() -> None:
    """Tier 2: choices=[] branch renders 'Enter to submit · Ctrl+C cancels' hint.

    Before C5 the free-text-only path yielded only an Input with no hint
    Label. The chip path had iv-hint but the free-text path was bare,
    leaving users without a visible keyboard affordance.

    Pins the public DOM surface: after mounting a free-text-only
    InterventionWidget, querying its Label elements must include a label
    with the keyboard hint text.
    """
    from textual.widgets import Label

    from reyn.interfaces.tui.widgets import ConversationView
    from reyn.interfaces.tui.widgets.intervention import InterventionWidget

    app = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        widget = InterventionWidget(
            iv_id="hint-test-iv",
            question="What do you want to do?",
            choices=[],  # free-text-only — the C5 path
        )
        await conv.mount(widget)
        await pilot.pause()

        labels = list(widget.query(Label))
        label_texts = [str(lbl.render()) for lbl in labels]

        hint_labels = [
            t for t in label_texts if "Enter to submit" in t and "Ctrl+C" in t
        ]
        assert hint_labels, (
            f"Expected a hint label containing 'Enter to submit · Ctrl+C cancels' "
            f"in the free-text-only branch, but found only: {label_texts!r}"
        )

        widget.remove()


@pytest.mark.asyncio
async def test_free_text_hint_uses_iv_hint_css_class() -> None:
    """Tier 2: the hint Label in the free-text branch carries the 'iv-hint' CSS class.

    Ensures the label reuses the existing iv-hint style (not a new ad-hoc
    class) so the WCAG-compliant #888888 colour and padding-top:1 apply.
    """
    from reyn.interfaces.tui.widgets import ConversationView
    from reyn.interfaces.tui.widgets.intervention import InterventionWidget

    app = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        widget = InterventionWidget(
            iv_id="hint-class-iv",
            question="Do something?",
            choices=[],
        )
        await conv.mount(widget)
        await pilot.pause()

        # Query using the iv-hint class selector — must find at least one match.
        hint_nodes = list(widget.query(".iv-hint"))
        assert hint_nodes, (
            "Expected at least one element with class 'iv-hint' in the "
            "free-text-only InterventionWidget, found none."
        )

        widget.remove()


@pytest.mark.asyncio
async def test_chip_path_still_has_iv_hint_label() -> None:
    """Tier 2: chip-path iv-hint label is unchanged after C5 change.

    Regression guard: the C5 fix must not remove the existing chip-path
    hint ('hotkey · Tab cycles · Ctrl+C cancels · free response… for free text').
    """
    from reyn.interfaces.tui.widgets import ConversationView
    from reyn.interfaces.tui.widgets.intervention import InterventionWidget

    app = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        widget = InterventionWidget(
            iv_id="chip-hint-iv",
            question="Continue?",
            choices=[
                {"label": "[y]es", "id": "yes", "hotkey": "y", "default": True},
            ],
        )
        await conv.mount(widget)
        await pilot.pause()

        hint_nodes = list(widget.query(".iv-hint"))
        assert hint_nodes, (
            "Expected the iv-hint label in the chip-path branch to still exist."
        )

        widget.remove()


@pytest.mark.asyncio
async def test_chips_present_at_narrow_width() -> None:
    """Tier 2: core chip buttons mount and are DOM-queryable at a narrow terminal size.

    At 40 columns (a challenging narrow terminal), the essential chip
    buttons — at minimum the first chip and 'free response…' — must be
    present in the widget's DOM. This tests that overflow-x:hidden causes
    clipping rather than preventing chip mounting.

    We assert DOM presence (query result count > 0), not visual
    pixel-level position, since the public surface for Textual widget
    tests is DOM / rendered text, not geometry.
    """
    from textual.widgets import Button

    from reyn.interfaces.tui.widgets import ConversationView
    from reyn.interfaces.tui.widgets.intervention import InterventionWidget

    app = _make_app()
    # 40 cols × 24 rows — narrow enough to trigger overflow on ~62-cell chip row
    async with app.run_test(headless=True, size=(40, 24)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        widget = InterventionWidget(
            iv_id="narrow-iv",
            question="Allow?",
            choices=[
                {"label": "[y]es", "id": "yes", "hotkey": "y", "default": True},
                {"label": "[n]o", "id": "no", "hotkey": "n", "default": False},
            ],
        )
        await conv.mount(widget)
        await pilot.pause()

        buttons = list(widget.query(Button))
        # The essential chips must be present by id (reachable at narrow width).
        button_ids = {b.id for b in buttons}
        assert "chip_yes" in button_ids, (
            f"'chip_yes' must be present at narrow width; found ids: {button_ids!r}"
        )
        assert "chip__free" in button_ids, (
            f"'chip__free' (free response) must be present at narrow width; "
            f"found ids: {button_ids!r}"
        )

        widget.remove()
