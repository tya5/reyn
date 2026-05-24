"""Tier 2: empty-state hint surfaces the side-panel tabs by name.

The right panel (Keys / Events / Agents / Memory / Cost / Docs) is
``display: none`` by default. Before this fix, the only on-screen cue
was the footer's terse ``Ctrl+B panel`` — a new user with no prior
context could not tell what the panel even contains, so the entire
right-panel subsystem was zero-discoverability on first launch.

Pins the contract that the empty-state hint:
  1. Names the slash mechanic (``/`` opens the command picker)
  2. Names the help command explicitly (``/help`` for orientation)
  3. Describes the side panel by enumerating its tabs

This is the first thing a new user sees; a future refactor that drops
back to ``Ctrl+B panel`` alone will regress discoverability silently.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from textual.widgets import Static

from reyn.chat.tui.app import ReynTUIApp


def _make_app() -> ReynTUIApp:
    return ReynTUIApp(
        registry=None,
        agent_name="test-agent",
        model="test-model",
        budget_tracker=None,
    )


@pytest.mark.asyncio
async def test_empty_hint_names_side_panel_and_tabs() -> None:
    """Tier 2b: empty-state hint enumerates the panel tabs by name."""
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        hint = app.query_one("#empty-hint", Static)
        text = str(hint.render())

        # Core mechanics
        assert "/" in text, text
        assert "/help" in text, text
        assert "Ctrl+B" in text, text

        # Panel discoverability — at least the key word + 2 tab names
        assert "side panel" in text.lower(), (
            f"hint must say 'side panel' for discoverability; got: {text!r}"
        )
        # Tabs — assert on at least three to defend against accidental dropouts
        for tab in ("keys", "events", "docs"):
            assert tab in text.lower(), f"tab {tab!r} missing from hint: {text!r}"


@pytest.mark.asyncio
async def test_empty_hint_hides_after_first_message() -> None:
    """Tier 2b: hint hides after first message — empty-conv-pane subsystem contract."""
    from reyn.chat.outbox import OutboxMessage
    from reyn.chat.tui.widgets import ConversationView

    app = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        hint = app.query_one("#empty-hint", Static)
        assert not hint.has_class("hidden")

        conv = app.query_one("#conversation", ConversationView)
        conv.render_message(OutboxMessage(kind="user", text="hi"))
        await pilot.pause()

        assert hint.has_class("hidden"), "hint should hide on first message"
