"""Tier 2: boundary hint writes sticky-only, not log (G-F9).

Wave-10 follow-up Topic G finding F9 (P2): ``_flash_boundary_hint``
wrote to BOTH the sticky status and the conv log. The log line
polluted scrollback — alternating Ctrl+P / Ctrl+N at boundaries
wrote two new ``↑ beginning`` / ``↓ end`` lines on every
direction change, accumulating in scroll history as navigation
artifacts indistinguishable from conversation content.

After the fix the log write is dropped; sticky-only matches the
``_flash_turn_position`` convention (which switched to sticky-
only after FS1). The sticky is docked at the conv pane bottom
and is ALWAYS visible regardless of scroll position.

Public surfaces tested:
  - boundary hint sets sticky body (= visible cue path)
  - boundary hint does NOT write a log line (= the pollution fix)
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


@pytest.mark.asyncio
async def test_start_boundary_hint_writes_sticky_only() -> None:
    """Tier 2: ``start`` boundary → sticky body set + log unchanged."""
    from reyn.tui.app import ReynTUIApp
    from reyn.tui.widgets import ConversationView

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        log = conv._log()
        pre_lines = len(getattr(log, "lines", []))

        conv._scroll_ctrl._flash_boundary_hint("start")
        await pilot.pause()

        snap = conv._sticky().snapshot()
        assert snap["active"] is True
        assert "beginning of history" in snap["body"]
        # Log line count must NOT have grown.
        post_lines = len(getattr(log, "lines", []))
        assert post_lines == pre_lines, (
            f"boundary hint should not write a log line; "
            f"pre={pre_lines} post={post_lines}"
        )


@pytest.mark.asyncio
async def test_end_boundary_hint_writes_sticky_only() -> None:
    """Tier 2: ``end`` boundary → sticky body set + log unchanged."""
    from reyn.tui.app import ReynTUIApp
    from reyn.tui.widgets import ConversationView

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        log = conv._log()
        pre_lines = len(getattr(log, "lines", []))

        conv._scroll_ctrl._flash_boundary_hint("end")
        await pilot.pause()

        snap = conv._sticky().snapshot()
        assert snap["active"] is True
        assert "end of history" in snap["body"]
        post_lines = len(getattr(log, "lines", []))
        assert post_lines == pre_lines


@pytest.mark.asyncio
async def test_repeated_direction_dedup_unchanged() -> None:
    """Tier 2b: rapid boundary repeats still deduped (regression)."""
    from reyn.tui.app import ReynTUIApp
    from reyn.tui.widgets import ConversationView

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        # First start-boundary fires.
        conv._scroll_ctrl._flash_boundary_hint("start")
        snap1 = conv._sticky().snapshot()
        body_after_first = snap1["body"]
        # Hide the sticky so we can tell whether the second call
        # re-shows it.
        conv.hide_status()
        conv._scroll_ctrl._flash_boundary_hint("start")
        snap2 = conv._sticky().snapshot()
        # Dedup → second call is a no-op → sticky stays hidden.
        assert snap2["active"] is False, (
            f"repeated start-boundary should dedup; sticky should stay "
            f"hidden, got body_after_first={body_after_first!r} "
            f"snap2={snap2!r}"
        )
