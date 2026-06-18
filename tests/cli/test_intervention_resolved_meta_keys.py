"""Tier 2: `_on_intervention_resolved` tolerates both meta-key shapes (E-F1).

Wave-9 Topic E finding F1 (P1): ``_on_intervention`` reads
``msg.meta["intervention_id"]`` while ``_on_intervention_resolved``
read ``msg.meta["iv_id"]``. The service emitted the two keys
consistently by convention, but a one-line rename on the service
side would silently break the resolve lookup — the
``query_one("#iv_…")`` would miss, the bare ``except Exception:
pass`` would swallow it, and the InterventionWidget would stay
mounted with no removal path.

The TUI now reads BOTH keys (``intervention_id`` preferred,
``iv_id`` fallback) so widget removal is immune to which key any
future producer chooses. Same id field, two historical names.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


async def _mount_intervention_widget(pilot, iv_id: str):
    """Mount an InterventionWidget under the conversation view."""
    from reyn.interfaces.tui.widgets import ConversationView
    from reyn.interfaces.tui.widgets.intervention import InterventionWidget

    app = pilot.app
    conv = app.query_one("#conversation", ConversationView)
    widget = InterventionWidget(iv_id=iv_id, question="confirm?")
    await conv.mount(widget)
    await pilot.pause()
    return widget


def _resolved_msg(meta: dict):
    """Build a minimal intervention_resolved OutboxMessage."""
    from reyn.runtime.outbox import OutboxMessage
    return OutboxMessage(kind="intervention_resolved", text="", meta=meta)


@pytest.mark.asyncio
async def test_resolved_handler_finds_widget_with_legacy_iv_id_key() -> None:
    """Tier 2: meta carrying only ``iv_id`` still removes the widget.

    The service emits ``meta={"iv_id": iv.id, …}`` on resolve today.
    The handler must continue to honor this legacy shape.
    """
    from reyn.interfaces.tui.app import ReynTUIApp
    from reyn.interfaces.tui.widgets.intervention import InterventionWidget

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        widget = await _mount_intervention_widget(pilot, iv_id="abc1efgh-xxxx")
        assert app.query(InterventionWidget)

        msg = _resolved_msg({"iv_id": "abc1efgh-xxxx"})
        from reyn.interfaces.tui.app_outbox import OutboxRouter
        router = OutboxRouter(app)
        conv = app.query_one("#conversation")
        header = app.query_one("#header")
        router._on_intervention_resolved(msg, conv, header)
        await pilot.pause()
        assert not app.query(InterventionWidget), (
            "widget should be removed when meta carries iv_id"
        )
        del widget  # keep linter quiet


@pytest.mark.asyncio
async def test_resolved_handler_finds_widget_with_intervention_id_key() -> None:
    """Tier 2: meta carrying ``intervention_id`` also removes the widget.

    If the service is ever refactored so the resolve emit uses the
    same ``intervention_id`` key as the mount emit, the TUI must not
    leak orphan widgets.
    """
    from reyn.interfaces.tui.app import ReynTUIApp
    from reyn.interfaces.tui.widgets.intervention import InterventionWidget

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        widget = await _mount_intervention_widget(pilot, iv_id="def2hijk-yyyy")
        assert app.query(InterventionWidget)

        msg = _resolved_msg({"intervention_id": "def2hijk-yyyy"})
        from reyn.interfaces.tui.app_outbox import OutboxRouter
        router = OutboxRouter(app)
        conv = app.query_one("#conversation")
        header = app.query_one("#header")
        router._on_intervention_resolved(msg, conv, header)
        await pilot.pause()
        assert not app.query(InterventionWidget), (
            "widget should be removed when meta carries intervention_id"
        )
        del widget


@pytest.mark.asyncio
async def test_resolved_handler_prefers_intervention_id_when_both_present() -> None:
    """Tier 2: ``intervention_id`` wins over ``iv_id`` when both meta keys exist.

    If a future emit carries both keys (= during a migration window),
    the canonical ``intervention_id`` should be the one consulted.
    Mount widget under ``intervention_id`` value; pass meta with a
    NONSENSE ``iv_id`` value alongside the correct ``intervention_id``
    — successful removal proves ``intervention_id`` was the key read.
    """
    from reyn.interfaces.tui.app import ReynTUIApp
    from reyn.interfaces.tui.widgets.intervention import InterventionWidget

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        widget = await _mount_intervention_widget(pilot, iv_id="ghi3lmno-zzzz")
        assert app.query(InterventionWidget)

        msg = _resolved_msg({
            "intervention_id": "ghi3lmno-zzzz",  # correct
            "iv_id": "WRONG_KEY_zzz",  # nonsense; would miss the widget
        })
        from reyn.interfaces.tui.app_outbox import OutboxRouter
        router = OutboxRouter(app)
        conv = app.query_one("#conversation")
        header = app.query_one("#header")
        router._on_intervention_resolved(msg, conv, header)
        await pilot.pause()
        assert not app.query(InterventionWidget), (
            "intervention_id should be preferred over iv_id"
        )
        del widget
