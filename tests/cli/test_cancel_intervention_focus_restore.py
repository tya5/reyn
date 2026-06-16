"""Tier 2: Ctrl+C intervention dismiss restores InputBar focus (E-F3).

Wave-9 Topic E finding F3 (P1): the Ctrl+C handler removed
InterventionWidget DOM nodes via ``conv.query(InterventionWidget)``
+ ``widget.remove()``. Unlike ``InterventionWidget._submit`` and
``_on_intervention_resolved`` — both of which explicitly restore
focus to the InputBar after removal — the Ctrl+C path had no
focus-restoration call. Textual's focus walker picked the next
focusable widget in DOM order, typically a SkillActivityRow or a
right-panel element. The user's next keystroke went nowhere and
they had to manually Tab back to the input bar.

After the fix, ``action_cancel_inflight`` calls
``InputBar.focus_input()`` whenever it dismissed at least one
InterventionWidget. The restore is gated on
``intervention_widgets_dismissed > 0`` so a Ctrl+C with no
intervention on screen doesn't steal focus from wherever the user
intentionally moved it.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


async def _mount_intervention(pilot, iv_id: str = "focus-test"):
    """Mount a free-text-only InterventionWidget under the conv pane."""
    from reyn.tui.widgets import ConversationView
    from reyn.tui.widgets.intervention import InterventionWidget

    app = pilot.app
    conv = app.query_one("#conversation", ConversationView)
    widget = InterventionWidget(iv_id=iv_id, question="confirm?", choices=[])
    await conv.mount(widget)
    await pilot.pause()
    return widget


@pytest.mark.asyncio
async def test_ctrl_c_after_intervention_focuses_input_bar() -> None:
    """Tier 2: Ctrl+C dismisses intervention + focuses the InputBar TextArea."""
    from textual.widgets import TextArea

    from reyn.tui.app import ReynTUIApp
    from reyn.tui.widgets.intervention import InterventionWidget

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        await _mount_intervention(pilot)
        # Move focus away from the input bar so we can observe the
        # restore (Textual auto-focuses the first focusable child on
        # mount — landing on the TextArea by default).
        chips_or_input = app.query("InterventionWidget > Input")
        if chips_or_input:
            chips_or_input[0].focus()
            await pilot.pause()

        # Sanity: at this point the focus is NOT the InputBar TextArea
        # (it was just moved to the intervention's Input).
        # Now Ctrl+C: action_cancel_inflight should dismiss + restore.
        app.action_cancel_inflight()
        await pilot.pause()

        assert not app.query(InterventionWidget), (
            "intervention widget should be removed after Ctrl+C"
        )
        focused = app.focused
        # The InputBar's TextArea is the focus target after restore.
        assert isinstance(focused, TextArea), (
            f"focus should be on the InputBar TextArea, got {focused!r}"
        )
        assert focused.id == "input", (
            f"focus should be on the #input TextArea inside InputBar, "
            f"got id={focused.id!r}"
        )


@pytest.mark.asyncio
async def test_ctrl_c_without_intervention_does_not_steal_focus() -> None:
    """Tier 2: Ctrl+C with no intervention on screen does not call focus_input.

    The restore is gated on at least one widget being dismissed — a
    bare Ctrl+C should not yank focus away from wherever the user
    intentionally moved it.
    """
    from reyn.tui.app import ReynTUIApp
    from reyn.tui.widgets import InputBar

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)

    focus_calls: list[None] = []

    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        bar = app.query_one("#inputbar", InputBar)

        # Spy on focus_input — we want to confirm it's NOT called when
        # there's no intervention to dismiss.
        original_focus = bar.focus_input

        def _spy() -> None:
            focus_calls.append(None)
            original_focus()

        bar.focus_input = _spy  # type: ignore[method-assign]

        app.action_cancel_inflight()
        await pilot.pause()
        assert focus_calls == [], (
            f"focus_input should not fire without an intervention dismissed: "
            f"{len(focus_calls)} call(s)"
        )
