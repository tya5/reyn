"""Tier 2: ErrorBox on_click is atomic on partial failure (I-F9).

Wave-10 follow-up Topic I finding F9 (P2): the previous
``on_click`` flipped ``_expanded`` BEFORE the DOM operations,
and only the header update was wrapped in ``try / except``. If
the header update raised, ``_expanded`` was already True but
the visible arrow stayed ``▶`` and the CSS class was already
toggled — the widget's internal state and visible DOM state
diverged. The next click would re-flip ``_expanded`` while the
DOM was still in the original state, doubling the drift.

After the fix the three operations (class toggle, ``_expanded``
flip, header update) are sequenced so any partial failure rolls
back the prior step. Net invariant: ``_expanded`` and the
``-expanded`` CSS class are always in lockstep.

Public surfaces tested:
  - normal click → ``_expanded`` flipped + class toggled +
    header updated (regression)
  - second normal click → both back to initial state (regression)
  - simulated header-update failure → ``_expanded`` NOT flipped
    (rollback)
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


@pytest.mark.asyncio
async def test_normal_click_toggles_expanded_and_class() -> None:
    """Tier 2b: the happy path still works (regression)."""
    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets import ConversationView

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        box = conv.mount_error(message="something broke")
        await pilot.pause()
        assert box.is_expanded is False
        assert not box.has_class("-expanded")

        box.on_click()
        await pilot.pause()
        assert box.is_expanded is True
        assert box.has_class("-expanded")

        # Second click toggles back.
        box.on_click()
        await pilot.pause()
        assert box.is_expanded is False
        assert not box.has_class("-expanded")


@pytest.mark.asyncio
async def test_header_update_failure_rolls_back_expanded() -> None:
    """Tier 2: header update raising leaves the widget in pre-click state.

    Simulate the failure by replacing ``header_text`` with a method
    that raises. The post-toggle internal state should match the
    pre-click state — no drift between ``_expanded`` and the CSS
    class.
    """
    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets import ConversationView

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        box = conv.mount_error(message="something broke")
        await pilot.pause()
        pre_expanded = box.is_expanded
        pre_class = box.has_class("-expanded")

        def _explode() -> str:
            raise RuntimeError("header update failed")

        box.header_text = _explode  # type: ignore[method-assign]
        box.on_click()
        await pilot.pause()

        # Both should match the pre-click state (rollback).
        assert box.is_expanded == pre_expanded, (
            f"is_expanded should rollback to {pre_expanded}, got {box.is_expanded}"
        )
        assert box.has_class("-expanded") == pre_class, (
            f"class should rollback to {pre_class}, got "
            f"{box.has_class('-expanded')}"
        )
