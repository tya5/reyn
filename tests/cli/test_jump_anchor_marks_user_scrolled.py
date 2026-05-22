"""Tier 2: _jump_to_relative_anchor marks the scroll as user-initiated (G-F4).

Wave-10 Topic G finding F4 (P2): ``scroll_page_up`` and
``scroll_line_up`` explicitly set ``_user_scrolled = True`` after
the scroll so the next incoming chunk's auto_scroll write doesn't
yank the view back to the tail. ``_jump_to_relative_anchor`` (=
Ctrl+P / Ctrl+N turn navigation) relied on the ``_on_log_scroll_y``
watcher to set the flag as a side effect of the scroll. The
watcher works for upward jumps but flips the flag back to False
whenever ``at_bottom`` evaluates True — which can happen when the
jump target sits within 1 line of ``max_scroll_y`` (= the last
anchor in a recent session). Result: Ctrl+P mid-stream → view
jumped → next chunk auto_scrolled → view snapped back to the
tail, interrupting the user's turn-navigation read.

Public surfaces tested:
  - ``_user_scrolled`` is True after a Ctrl+P / Ctrl+N jump
  - the flag is set regardless of whether the watcher's at_bottom
    check fires (= explicit set in the jump function, not relying
    on the watcher path)
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


@pytest.mark.asyncio
async def test_jump_to_relative_anchor_marks_user_scrolled_true() -> None:
    """Tier 2: a successful jump (= scroll actually moves) sets the flag.

    Drives ``_jump_to_relative_anchor`` against synthetic anchors that
    sit well above the current scroll position so the function takes
    the scroll branch (not the ``hit_boundary + abs(cur_y - target) <= 1``
    early-return branch). The flag set is the load-bearing change —
    pre-fix the function relied on the ``_on_log_scroll_y`` watcher
    to set it as a side effect, which silently flipped back to False
    when ``at_bottom`` evaluated True near the last anchor.
    """
    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets import ConversationView

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        log = conv._log()

        # Synthetic anchors well above the viewport. ``log._start_line``
        # is 0 (no ring-buffer trim), so absolute positions == relative
        # positions in ``_resolve_anchors_to_current_view``.
        conv._turn_anchors = [5, 50, 100]
        # Move the log scroll past the first anchor so a backward
        # jump has somewhere to land.
        try:
            log.scroll_to(y=100, animate=False)
        except Exception:
            pass
        await pilot.pause()

        # Pre-jump state: user has not scrolled.
        conv._user_scrolled = False

        # Ctrl+P → backward jump from 100 → anchor 50.
        conv._jump_to_relative_anchor(-1)
        await pilot.pause()

        assert conv._user_scrolled is True, (
            "_jump_to_relative_anchor(-1) must mark user_scrolled True "
            "so the next chunk's auto_scroll doesn't snap back to the tail"
        )


@pytest.mark.asyncio
async def test_jump_keeps_user_scrolled_even_on_forward_to_last_anchor() -> None:
    """Tier 2: forward jump to last anchor still sets the flag.

    The pre-fix bug was specifically that ``_on_log_scroll_y``'s
    ``at_bottom`` check flipped the flag back to False on a jump
    that landed near the tail. The explicit ``self._user_scrolled =
    True`` in the jump function prevents the watcher's reset from
    being the load-bearing signal.
    """
    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets import ConversationView

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        log = conv._log()

        # Anchors near tail; current position above the last anchor.
        conv._turn_anchors = [5, 50, 100]
        try:
            log.scroll_to(y=10, animate=False)
        except Exception:
            pass
        await pilot.pause()

        conv._user_scrolled = False
        # Ctrl+N → forward jump from 10 → anchor 50.
        conv._jump_to_relative_anchor(+1)
        await pilot.pause()

        assert conv._user_scrolled is True, (
            "_jump_to_relative_anchor(+1) must mark user_scrolled True "
            "regardless of where the jump lands relative to the tail"
        )
