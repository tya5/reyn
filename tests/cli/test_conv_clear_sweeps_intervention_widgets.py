"""Tier 2: ConversationView.clear() removes mounted InterventionWidget (G-F2).

Wave-10 Topic G finding F2 (P1): ``clear()`` swept
``_stream_rows`` / ``_skill_rows`` / ``_tool_call_rows``, but missed
``InterventionWidget``. ``mount_intervention`` adds the widget
via ``self.mount(widget)`` with no tracking list. After Ctrl+L
the widget stayed on the now-blank pane and the user could still
click chip buttons → fired the answer_callback against a session
context they just cleared (= acting on stale UI state).

``clear()`` now queries ``self.query(InterventionWidget)`` and
removes each. No tracking list is added — the per-clear ``query``
cost is negligible compared with the ``log.clear()`` call right
next to it. (Errors are now plain RichLog lines so ``_log().clear()``
already removes them — no separate sweep needed.)

Public surfaces tested:
  - InterventionWidget present before clear() is gone after
  - clear() with no intervention mounted is a no-op (regression
    guard for the empty-query path)
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


@pytest.mark.asyncio
async def test_clear_removes_pending_intervention_widget() -> None:
    """Tier 2: clear() unmounts a pending InterventionWidget."""
    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets import ConversationView
    from reyn.chat.tui.widgets.intervention import InterventionWidget

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        conv.mount_intervention(
            question="proceed?",
            choices=[{"label": "[y]es", "id": "yes", "hotkey": "y"}],
            iv_id="iv-clear-test",
        )
        await pilot.pause()
        # Sanity: widget mounted before clear.
        assert app.query(InterventionWidget), (
            "test scaffolding broken — InterventionWidget should be mounted"
        )

        conv.clear()
        await pilot.pause()

        assert not app.query(InterventionWidget), (
            "InterventionWidget should be removed from DOM after clear()"
        )


@pytest.mark.asyncio
async def test_clear_with_no_intervention_is_safe() -> None:
    """Tier 2: clear() with no intervention mounted completes without error.

    Regression guard: the ``for widget in list(self.query(...))`` empty
    iteration must not raise.
    """
    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets import ConversationView
    from reyn.chat.tui.widgets.intervention import InterventionWidget

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        assert not app.query(InterventionWidget)
        conv.clear()  # must not raise
        await pilot.pause()
        assert not app.query(InterventionWidget)
