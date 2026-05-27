"""Tier 2: scroll_to_bottom drops the dead _user_scrolled assignment (G-F14).

Wave-10 follow-up Topic G finding F14 (P3): ``scroll_to_bottom``
called ``self._snap_to_bottom()`` and then set
``self._user_scrolled = False`` — a dead instruction since
``_snap_to_bottom`` already unconditionally resets the flag. The
duplicate misled future readers ("does ``_snap_to_bottom`` not
reset it? else why the second set?"), so removing it is pure
code hygiene with zero behaviour change.

Public surfaces tested:
  - ``scroll_to_bottom`` still leaves ``_user_scrolled = False``
    (= behaviour unchanged, the flag set is now delegated to
    ``_snap_to_bottom``)
  - source-level check confirms the dead assignment is gone
"""
from __future__ import annotations

import inspect
import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


@pytest.mark.asyncio
async def test_scroll_to_bottom_still_resets_user_scrolled_flag() -> None:
    """Tier 2: behaviour unchanged — ``_user_scrolled = False`` after call."""
    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets import ConversationView

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        # Simulate the user having scrolled up at some point.
        conv._user_scrolled = True
        conv.scroll_to_bottom()
        await pilot.pause()
        assert conv.user_scrolled is False, (
            "scroll_to_bottom must still leave user_scrolled False; "
            "the flag set was delegated to _snap_to_bottom, not removed"
        )


def test_scroll_to_bottom_source_has_no_duplicate_flag_set() -> None:
    """Tier 2b: the body has exactly one path to flag-reset (source-level).

    Pins the cleanup so a future refactor can't silently re-add the
    duplicate. The ``_user_scrolled = False`` line should appear at
    most ONCE in ``_snap_to_bottom``'s body, never in
    ``scroll_to_bottom``'s.
    """
    from reyn.chat.tui.widgets.conversation import ConversationView

    snap_src = inspect.getsource(ConversationView._snap_to_bottom)
    scroll_src = inspect.getsource(ConversationView.scroll_to_bottom)
    assert "self._user_scrolled = False" in snap_src, (
        "_snap_to_bottom should still own the flag-reset (regression "
        "guard for the delegated behaviour)"
    )
    assert "self._user_scrolled = False" not in scroll_src, (
        "scroll_to_bottom should not duplicate the flag-reset that "
        "_snap_to_bottom already performs"
    )
