"""Tier 2: focus restoration paths after Esc / intervention dismissal.

Two MED-severity findings from the focus-management UX audit collapsed
to "focus needs an explicit hand-off; without it Textual's auto-walker
lands on a non-editable widget and the user types into nothing":

  • Focus F3 — Esc inside the right panel was a silent no-op. Tab and
    Shift+Tab cycle panel tabs (= focus trap), and the App's escape
    binding is gated by ``check_action`` which returns False when no
    error box / voice recording is interceptable.

  • Focus F4 — InterventionWidget.remove() left focus on whatever DOM
    walker picked next (commonly a child of ConversationView or, when
    the panel was open, a panel tab strip), neither of which accept
    typed input.

These tests pin both fixes by exercising the user-visible flow with
the headless ``Pilot`` and asserting that focus lands on the input
TextArea after each action.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from textual.widgets import TextArea

from reyn.chat.tui.app import ReynTUIApp
from reyn.chat.tui.widgets import ConversationView


def _make_app() -> ReynTUIApp:
    return ReynTUIApp(
        registry=None,
        agent_name="test-agent",
        model="test-model",
        budget_tracker=None,
    )


# ── Focus F3 — Esc in right panel restores focus to input ────────────────────


@pytest.mark.asyncio
async def test_escape_from_right_panel_focuses_input() -> None:
    """Tier 2b: Esc inside the right panel returns focus to the input TextArea.

    Reproduces the previous trap: the user opens the panel, focus moves
    into it, and the standard "back out" key (Esc) used to do nothing.
    """
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()

        # Open the panel — PC1 (PR #345): Ctrl+B now auto-focuses
        # panel tabs on open, so the separate Ctrl+O hop is no longer
        # part of the setup. See ``action_toggle_panel`` in app.py.
        await pilot.press("ctrl+b")
        await pilot.pause()

        # Sanity: focus is no longer on the input TextArea
        ta = app.query_one("#input", TextArea)
        assert app.focused is not ta, (
            "test setup failed: focus did not move into the right panel"
        )

        # Esc → focus must return to input
        await pilot.press("escape")
        await pilot.pause()
        assert app.focused is ta, (
            f"Esc from right panel did not restore input focus; got {app.focused}"
        )


# ── Focus F4 — InterventionWidget removal restores focus to input ────────────


@pytest.mark.asyncio
async def test_intervention_widget_submit_restores_input_focus() -> None:
    """Tier 2b: After ``InterventionWidget._submit`` removes itself, focus is on input.

    The chip-button submit path explicitly calls ``focus_input()`` after
    ``self.remove()``. Without that, Textual's auto-focus walker can
    land on a peer widget (most often a child of ConversationView) and
    silently kill typing.
    """
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)

        callback_calls: list[str] = []

        async def _callback(answer: str) -> None:
            callback_calls.append(answer)

        widget = conv.mount_intervention(
            question="proceed?",
            choices=None,
            answer_callback=_callback,
            iv_id="iv_focus_test",
        )
        await pilot.pause()
        assert widget is not None

        # Submit the intervention with free-text answer
        await widget._submit("yes")
        await pilot.pause()
        await pilot.pause()

        assert callback_calls == ["yes"]

        # Focus must be on the input TextArea after submit + remove
        ta = app.query_one("#input", TextArea)
        assert app.focused is ta, (
            f"intervention submit did not restore input focus; "
            f"got focus on {app.focused}"
        )


@pytest.mark.asyncio
async def test_intervention_resolved_handler_restores_input_focus() -> None:
    """Tier 2b: The text-input route ``_on_intervention_resolved`` also restores focus.

    Mirrors the chip-button path: when the user answers an intervention
    by typing into the InputBar (Enter routes through the session, not
    through ``_submit``), the resolved outbox message removes the widget
    and must hand focus back the same way.
    """
    from reyn.chat.outbox import OutboxMessage
    from reyn.chat.tui.app_outbox import OutboxRouter
    from reyn.chat.tui.widgets import ReynHeader

    app = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        header = app.query_one("#header", ReynHeader)

        async def _callback(_answer: str) -> None:
            pass

        conv.mount_intervention(
            question="proceed?",
            choices=None,
            answer_callback=_callback,
            iv_id="iv_xyz12345",
        )
        await pilot.pause()

        # Drive the resolved handler directly — that's the path the
        # session/outbox flow takes after text-input delivery.
        router = OutboxRouter(app)
        router._on_intervention_resolved(
            OutboxMessage(kind="intervention_resolved", text="", meta={"iv_id": "xyz12345"}),
            conv, header,
        )
        await pilot.pause()
        await pilot.pause()

        ta = app.query_one("#input", TextArea)
        assert app.focused is ta, (
            f"intervention_resolved did not restore input focus; got {app.focused}"
        )
