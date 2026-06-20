"""Tier 2: a resolving intervention must not steal focus from a still-pending one.

When interventions queue, the registry announces + mounts the next one as the
current resolves. `_on_intervention_resolved` restored focus to the main
InputBar UNCONDITIONALLY — so if the user was typing into a still-mounted
intervention's free-input (`#iv_input`), an unrelated earlier intervention
resolving yanked their cursor to the main input mid-answer.

Fix: only restore focus to the main input when no OTHER intervention widget
remains (the just-removed one is excluded so its async removal doesn't falsely
count as pending).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from reyn.interfaces.tui.app import ReynTUIApp
from reyn.interfaces.tui.app_outbox import OutboxRouter
from reyn.interfaces.tui.widgets import ConversationView, ReynHeader
from reyn.interfaces.tui.widgets.intervention import InterventionWidget
from reyn.runtime.outbox import OutboxMessage


def _make_app() -> ReynTUIApp:
    return ReynTUIApp(
        registry=None, agent_name="t", model="m", budget_tracker=None,
    )


@pytest.mark.asyncio
async def test_resolved_does_not_steal_focus_from_pending_free_input() -> None:
    """Tier 2: resolving intervention A keeps focus on pending B's free-input."""
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        header = app.query_one("#header", ReynHeader)
        router = OutboxRouter(app)

        # Intervention B is mounted and the user opened its free-form input.
        app._mount_intervention(conv, "Question B?", "ivB00000", None)
        await pilot.pause()
        widget_b = next(iter(app.query(InterventionWidget)))
        widget_b._show_free_input()
        await pilot.pause()
        assert getattr(app.focused, "id", None) == "iv_input", (
            "precondition: the user is focused in B's free-input"
        )

        # A DIFFERENT intervention (A) resolves — its outbox arrives.
        router._on_intervention_resolved(
            OutboxMessage(
                kind="intervention_resolved", text="",
                meta={"intervention_id": "ivA99999"},
            ),
            conv, header,
        )
        await pilot.pause()

        assert getattr(app.focused, "id", None) == "iv_input", (
            "focus must stay in the still-pending intervention's free-input, "
            "not be yanked to the main input by an unrelated resolution"
        )


@pytest.mark.asyncio
async def test_resolved_restores_focus_when_no_intervention_remains() -> None:
    """Tier 2: with no other intervention pending, focus returns to the input."""
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        header = app.query_one("#header", ReynHeader)
        router = OutboxRouter(app)

        # A single intervention A; it resolves and its widget is removed.
        app._mount_intervention(conv, "Question A?", "ivA00000", None)
        await pilot.pause()

        router._on_intervention_resolved(
            OutboxMessage(
                kind="intervention_resolved", text="",
                meta={"intervention_id": "ivA00000"},
            ),
            conv, header,
        )
        await pilot.pause()

        assert getattr(app.focused, "id", None) == "input", (
            "with no intervention left, focus must return to the main input "
            "(no regression to the lone-intervention restore)"
        )
