"""Tier 2: F5 / F6 jump to prev / next mounted ErrorBox in the conv pane.

Categorical UX gap on the debugging axis. In sessions with
multiple mounted ErrorBox widgets (= 2+ errors stacked under the
conv pane), the user had no quick way to scroll between them —
manual PageUp / PageDown or fine-grained cursor scroll was the
only path. This adds:

  - F5 → jump to previous (older) error
  - F6 → jump to next (newer) error
  - First press from fresh state targets the NEWEST error (=
    almost always the one the user wants first)
  - Cycle with wrap; cursor invalidated on dismiss / auto-eviction
  - Status hint when no errors are mounted ("no errors to jump to")

Pinned:
  - ``jump_to_error(direction)`` returns False with no errors,
    True after a scroll
  - First call targets the newest error (= ``len-1`` index)
  - Subsequent calls cycle with wrap (+1 wraps from last → first,
    -1 wraps from first → last)
  - ``dismiss_last_error()`` resets the cursor to -1
  - Auto-eviction in ``mount_error`` resets the cursor
  - F5 / F6 bindings registered with correct action names
  - Keys tab routes F5 / F6 to CONVERSATION + pretty-prints
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


@pytest.mark.asyncio
async def test_jump_to_error_no_errors_returns_false() -> None:
    """Tier 2: ``jump_to_error`` with empty list returns False."""
    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets import ConversationView

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        assert conv.jump_to_error(+1) is False
        assert conv.jump_to_error(-1) is False


@pytest.mark.asyncio
async def test_first_jump_targets_newest_error() -> None:
    """Tier 2: first F5/F6 press lands on the newest (last-in-list) error."""
    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets import ConversationView

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        conv.mount_error(message="first error")
        conv.mount_error(message="second error")
        conv.mount_error(message="third error")
        await pilot.pause()
        # Cursor unset → both directions land on the newest.
        assert conv.error_jump_cursor() == -1
        assert conv.jump_to_error(+1) is True
        # Cursor advanced to last index (= len-1 = 2 for 3 errors).
        assert conv.error_jump_cursor() == 2


@pytest.mark.asyncio
async def test_cycle_forward_wraps_to_first() -> None:
    """Tier 2: F6 past the last error wraps back to the first."""
    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets import ConversationView

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        conv.mount_error(message="a")
        conv.mount_error(message="b")
        conv.mount_error(message="c")
        await pilot.pause()
        # Seed at newest (index 2), advance forward → 0 (wrap).
        conv.jump_to_error(+1)
        assert conv.error_jump_cursor() == 2
        conv.jump_to_error(+1)
        assert conv.error_jump_cursor() == 0


@pytest.mark.asyncio
async def test_cycle_backward_steps_correctly() -> None:
    """Tier 2: F5 walks backwards through the list with wrap."""
    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets import ConversationView

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        conv.mount_error(message="a")
        conv.mount_error(message="b")
        conv.mount_error(message="c")
        await pilot.pause()
        # Seed at newest, walk backward → 1, then 0.
        conv.jump_to_error(-1)
        assert conv.error_jump_cursor() == 2
        conv.jump_to_error(-1)
        assert conv.error_jump_cursor() == 1
        conv.jump_to_error(-1)
        assert conv.error_jump_cursor() == 0
        # One more → wraps to last.
        conv.jump_to_error(-1)
        assert conv.error_jump_cursor() == 2


@pytest.mark.asyncio
async def test_dismiss_last_error_resets_cursor() -> None:
    """Tier 2: dismissing an error invalidates the jump cursor."""
    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets import ConversationView

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        conv.mount_error(message="a")
        conv.mount_error(message="b")
        await pilot.pause()
        conv.jump_to_error(+1)
        assert conv.error_jump_cursor() == 1
        conv.dismiss_last_error()
        await pilot.pause()
        # Cursor reset.
        assert conv.error_jump_cursor() == -1


@pytest.mark.asyncio
async def test_action_jump_with_no_errors_shows_hint() -> None:
    """Tier 2: F5/F6 with no errors → status hint."""
    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets import ConversationView

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        # No errors mounted.
        app.action_jump_next_error()
        await pilot.pause()
        snap = conv._sticky().snapshot()  # type: ignore[union-attr]
        assert snap["active"] is True
        assert "no errors" in snap["body"]


@pytest.mark.asyncio
async def test_action_jump_advances_cursor() -> None:
    """Tier 2: ``action_jump_next_error`` advances the cursor + scrolls."""
    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets import ConversationView

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        conv.mount_error(message="a")
        conv.mount_error(message="b")
        await pilot.pause()
        app.action_jump_next_error()
        await pilot.pause()
        # First press → newest (index 1).
        assert conv.error_jump_cursor() == 1
        app.action_jump_next_error()
        await pilot.pause()
        # Wrap to index 0.
        assert conv.error_jump_cursor() == 0


def test_f5_f6_bindings_registered() -> None:
    """Tier 2: ``f5`` and ``f6`` are bound to error-jump actions."""
    from reyn.chat.tui.app import ReynTUIApp

    binds = {(b.key, b.action) for b in ReynTUIApp.BINDINGS}
    assert ("f5", "jump_prev_error") in binds
    assert ("f6", "jump_next_error") in binds


def test_keys_tab_routes_f5_f6_to_conversation() -> None:
    """Tier 2: F5 / F6 land in the CONVERSATION group + pretty-print."""
    from reyn.chat.tui.widgets.right_panel.keys_tab import (
        _key_group_for,
        _pretty_key,
    )

    assert _key_group_for("f5") == "CONVERSATION"
    assert _key_group_for("f6") == "CONVERSATION"
    assert _pretty_key("f5") == "F5"
    assert _pretty_key("f6") == "F6"


@pytest.mark.asyncio
async def test_keys_tab_render_includes_f5_f6_descriptions() -> None:
    """Tier 2: rendered Keys tab markup surfaces F5 / F6 descriptions."""
    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets.right_panel.keys_tab import render_keys

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        markup, _keys, _ = render_keys(app)
        assert "F5" in markup
        assert "F6" in markup
        assert "error" in markup.lower()
