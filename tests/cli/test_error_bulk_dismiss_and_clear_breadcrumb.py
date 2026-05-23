"""Tier 2: ErrorBox bulk-dismiss (Shift+Esc) + Ctrl+L error breadcrumb.

Wave-13 findings B#1 + C#1.

B#1: ``dismiss_all_errors`` clears every stacked ErrorBox in one
call and emits a single summary breadcrumb to the conv log so the
audit trail survives without flooding the log with N individual
dismissed lines.

C#1: ``clear()`` (Ctrl+L) emits a dim breadcrumb AFTER wiping the
log so the user knows N errors were present when they cleared,
with a pointer to the events tab for full context.

Pinned tests:
  1. 3 ErrorBoxes → dismiss_all_errors() → has_error_boxes() False
     AND log contains "✗ 3 errors dismissed".
  2. 1 ErrorBox → dismiss_all_errors() → breadcrumb uses singular
     "1 error dismissed".
  3. 2 ErrorBoxes → clear() → log contains
     "✗ 2 errors cleared (see events".
  4. clear() with 0 ErrorBoxes → no breadcrumb emitted.
  5. shift+escape binding routes to action_dismiss_all_errors
     (= method registered on ReynTUIApp).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def _log_text(log) -> str:
    """Concatenate all strip text in the RichLog for substring searches."""
    return "\n".join("".join(seg.text for seg in strip) for strip in log.lines)


@pytest.mark.asyncio
async def test_dismiss_all_errors_clears_three_boxes() -> None:
    """Tier 2: 3 ErrorBoxes → dismiss_all_errors() → no boxes remain + breadcrumb."""
    from textual.widgets import RichLog

    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets import ConversationView

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        for i in range(3):
            conv.mount_error(message=f"error {i}")
        await pilot.pause()
        assert conv.has_error_boxes()

        conv.dismiss_all_errors()
        await pilot.pause()

        assert not conv.has_error_boxes(), "dismiss_all_errors must clear all boxes"
        log = conv.query_one("#log", RichLog)
        content = _log_text(log)
        assert "✗ 3 errors dismissed" in content, (
            f"breadcrumb missing from log; got: {content!r}"
        )


@pytest.mark.asyncio
async def test_dismiss_all_errors_singular_noun() -> None:
    """Tier 2: 1 ErrorBox → dismiss_all_errors() → breadcrumb says '1 error dismissed'."""
    from textual.widgets import RichLog

    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets import ConversationView

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        conv.mount_error(message="solo error")
        await pilot.pause()

        conv.dismiss_all_errors()
        await pilot.pause()

        assert not conv.has_error_boxes()
        log = conv.query_one("#log", RichLog)
        content = _log_text(log)
        assert "1 error dismissed" in content, (
            f"singular noun expected; got: {content!r}"
        )
        # Must NOT use plural form.
        assert "1 errors dismissed" not in content


@pytest.mark.asyncio
async def test_clear_emits_breadcrumb_when_errors_present() -> None:
    """Tier 2: 2 ErrorBoxes → clear() → log contains breadcrumb pointing at events tab."""
    from textual.widgets import RichLog

    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets import ConversationView

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        conv.mount_error(message="err A")
        conv.mount_error(message="err B")
        await pilot.pause()

        conv.clear()
        await pilot.pause()

        log = conv.query_one("#log", RichLog)
        content = _log_text(log)
        assert "✗ 2 errors cleared" in content, (
            f"clear breadcrumb missing; got: {content!r}"
        )
        assert "see events" in content, (
            f"events tab pointer missing; got: {content!r}"
        )


@pytest.mark.asyncio
async def test_clear_no_breadcrumb_when_no_errors() -> None:
    """Tier 2: clear() with 0 ErrorBoxes → no spurious error breadcrumb."""
    from textual.widgets import RichLog

    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets import ConversationView

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        # No errors mounted — just clear.
        conv.clear()
        await pilot.pause()

        log = conv.query_one("#log", RichLog)
        content = _log_text(log)
        assert "errors cleared" not in content, (
            f"spurious breadcrumb present; got: {content!r}"
        )
        assert "error cleared" not in content


def test_shift_escape_action_registered() -> None:
    """Tier 2: action_dismiss_all_errors method exists on ReynTUIApp.

    Verifies that the Shift+Esc binding target is callable on the app
    class (= the binding won't silently no-op due to a missing action
    method). Does not require a running pilot.
    """
    from reyn.chat.tui.app import ReynTUIApp

    assert callable(getattr(ReynTUIApp, "action_dismiss_all_errors", None)), (
        "action_dismiss_all_errors must be defined on ReynTUIApp for the "
        "shift+escape binding to route correctly"
    )
