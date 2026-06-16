"""Tier 2: InterventionWidget keyboard accessibility (wave-6 IV1 + IV2).

Pins the contract that mounted interventions are keyboard-reachable
without the user having to discover ``Ctrl+O`` to cycle focus into the
chip area, and that ``Ctrl+C`` (= ``action_cancel_inflight``) dismisses
a pending intervention.

Before this work:
  - InputBar's TextArea held focus by default and consumed printable
    keys ('y' / 'A' / etc.) into its editor buffer before the bubble
    reached the InterventionWidget's ``on_key`` handler, so chip
    hotkeys were dead and the only keyboard path was ``Ctrl+O`` + Tab.
  - ``action_cancel_inflight`` only iterated ``session.running_skills``
    / ``running_plans`` / streams. A pending intervention (= modal
    waiting for an answer) was invisible to the cancel path, so
    ``Ctrl+C`` reported ``nothing in-flight to cancel`` and the modal
    stayed up indefinitely.

Tier 2 contracts pinned here:

1. Mounting an InterventionWidget with chips auto-focuses the first
   chip Button. Keyboard hotkeys reach ``on_key`` without further
   focus manipulation.
2. ``action_cancel_inflight`` removes any mounted InterventionWidget
   from the conversation view. (Cancelling the iv future via the
   session's InterventionRegistry is exercised in the integration
   path; the widget removal is the user-visible half pinned here.)
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from textual.widgets import Button

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from reyn.interfaces.tui.app import ReynTUIApp
from reyn.interfaces.tui.widgets import ConversationView
from reyn.interfaces.tui.widgets.intervention import InterventionWidget


def _make_app() -> ReynTUIApp:
    return ReynTUIApp(
        registry=None,
        agent_name="test-agent",
        model="test-model",
        budget_tracker=None,
    )


# ── IV1: auto-focus first chip on mount ───────────────────────────────


@pytest.mark.asyncio
async def test_intervention_auto_focuses_first_chip_on_mount() -> None:
    """Tier 2: mounting an iv with chips moves focus to the first Button.

    Without this, the InputBar's TextArea retains focus and consumes
    chip hotkey keystrokes before they reach the iv widget.
    """
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        widget = conv.mount_intervention(
            question="Continue?",
            choices=[
                {"label": "[y]es", "id": "yes", "hotkey": "y", "default": True},
                {"label": "[n]o", "id": "no", "hotkey": "n", "default": False},
            ],
            iv_id="iv-focus-test",
        )
        # Mount fires before our on_mount; pause to let composition + focus settle.
        await pilot.pause()
        await pilot.pause()

        focused = app.focused
        assert isinstance(focused, Button), (
            f"expected first chip Button to be focused, got: {focused!r}"
        )
        # First chip should be the "yes" chip.
        assert (focused.id or "").startswith("chip_yes"), (
            f"expected first chip to be 'chip_yes', got: {focused.id!r}"
        )

        # Cleanup — remove so we don't dangle.
        widget.remove()


@pytest.mark.asyncio
async def test_intervention_without_chips_does_not_grab_focus() -> None:
    """Tier 2: no-chip iv (= free-text only) follows the legacy path —
    the embedded Input field auto-focuses (existing behaviour), and
    the on_mount focus-shift is skipped for the chip-less branch.
    """
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        widget = conv.mount_intervention(
            question="Any thoughts?", choices=None, iv_id="iv-no-chip",
        )
        await pilot.pause()
        await pilot.pause()

        # The on_mount path early-returns when ``self._choices`` is
        # empty; whatever Textual's default focus walker chose is fine,
        # what matters is no Button was force-focused (= no Button
        # exists on this widget).
        buttons = list(widget.query(Button))
        assert buttons == [], "no-chip iv should not mount any Button"

        widget.remove()


# ── IV2: action_cancel_inflight removes mounted InterventionWidget ────


@pytest.mark.asyncio
async def test_cancel_inflight_removes_mounted_intervention_widget() -> None:
    """Tier 2: ``action_cancel_inflight`` clears any mounted iv widget.

    Without a session attached the cancel path's session iteration is a
    no-op, but the conv-side widget removal must still fire so the user
    sees the modal disappear when they press Ctrl+C. Pins the visible
    half of the cancel contract; the iv future cancellation is
    exercised at the InterventionRegistry layer.
    """
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        widget = conv.mount_intervention(
            question="Allow?",
            choices=[
                {"label": "[y]es", "id": "yes", "hotkey": "y", "default": True},
                {"label": "[n]o", "id": "no", "hotkey": "n", "default": False},
            ],
            iv_id="iv-cancel-test",
        )
        await pilot.pause()
        assert widget in list(conv.query(InterventionWidget))

        # Drive the same code path Ctrl+C fires.
        app.action_cancel_inflight()
        await pilot.pause()
        await pilot.pause()

        mounted = list(conv.query(InterventionWidget))
        assert mounted == [], (
            f"expected no InterventionWidget mounted after cancel; got: {mounted!r}"
        )
