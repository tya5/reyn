"""Tier 2: turn-nav anchors survive RichLog ring-buffer truncation.

Before this fix ``_turn_anchors`` stored bare ``len(log.lines)`` values
at write time. RichLog uses a ring buffer; the moment a session crosses
``max_lines`` the buffer trims the oldest lines and rebases
``log.lines``. Stored anchors silently rotted — Ctrl+P/N would land on
whatever line happened to occupy the original slot, not the actual
turn header.

The fix stores absolute write-positions
(``log._start_line + len(log.lines)``) and projects them back to current
``log.lines`` indexes on read, dropping anchors whose targets have been
trimmed. This test pins:
  1. Anchors written before trim survive a forced ring-buffer trim
  2. Anchors whose target was trimmed are silently filtered (= jump
     still works, picks the nearest surviving anchor)
  3. ``/clear`` resets the trim-warning latch so subsequent trims warn again
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from rich.text import Text
from textual.widgets import RichLog

from reyn.chat.tui.app import ReynTUIApp
from reyn.chat.tui.widgets import ConversationView


def _make_app() -> ReynTUIApp:
    return ReynTUIApp(
        registry=None,
        agent_name="test-agent",
        model="test-model",
        budget_tracker=None,
    )


def _force_trim(log: RichLog, lines_to_drop: int) -> None:
    """Force RichLog to behave as if ``lines_to_drop`` lines have been trimmed.

    The unit tests don't actually write 20,000+ lines into the log; they
    just nudge the public-effect of trim — ``_start_line`` going up while
    ``log.lines`` stays trimmed — by writing a few lines and then mutating
    ``_start_line`` directly. RichLog reads ``_start_line`` to compute
    drops, so this mirrors what happens organically at scale.
    """
    # Ensure there are some lines first
    for i in range(5):
        log.write(Text(f"baseline line {i}"))
    # Simulate that ``lines_to_drop`` older lines have already been ring-buffered out
    log._start_line = lines_to_drop


@pytest.mark.asyncio
async def test_anchor_survives_partial_trim_and_projects_correctly() -> None:
    """An anchor whose line is still in the buffer after partial trim resolves correctly.

    Sets up an anchor at a high absolute position (after baseline content
    is in the log), then simulates a partial trim that removes some earlier
    lines but leaves the anchor's target within the live buffer. The
    anchor must project to ``absolute - start_line``.
    """
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        log = conv.query_one(RichLog)

        # Lay down baseline content so the header anchor lands at a non-zero
        # absolute position.
        for i in range(20):
            log.write(Text(f"baseline {i}"))
        await pilot.pause()

        # Now record a header anchor.
        conv._maybe_write_header("user", "you ", "bold", "dim")
        await pilot.pause()
        assert len(conv._turn_anchors) == 1
        anchor_abs = conv._turn_anchors[0]
        assert anchor_abs >= 20, f"expected anchor past baseline, got {anchor_abs}"

        # Simulate a trim that pushes _start_line up by less than the anchor.
        log._start_line += 5

        # Stored anchor stays constant (= absolute storage).
        assert conv._turn_anchors[0] == anchor_abs

        # Projection subtracts the trim offset.
        resolved = conv._resolve_anchors_to_current_view(log)
        assert resolved == [anchor_abs - 5], (
            f"projection wrong after partial trim; got {resolved}"
        )


@pytest.mark.asyncio
async def test_anchor_dropped_when_target_trimmed() -> None:
    """An anchor whose line was trimmed out of the buffer is filtered."""
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        log = conv.query_one(RichLog)

        # Write a header at absolute position N.
        conv._maybe_write_header("user", "you ", "bold", "dim")
        await pilot.pause()
        anchor = conv._turn_anchors[0]

        # Trim past the anchor — its target line has been ring-buffered out.
        log._start_line = anchor + 10

        resolved = conv._resolve_anchors_to_current_view(log)
        assert resolved == [], (
            f"trimmed anchor must be filtered; got {resolved}"
        )


@pytest.mark.asyncio
async def test_jump_no_crash_when_all_anchors_trimmed() -> None:
    """Ctrl+P/N with every anchor trimmed must not raise (only warn)."""
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        log = conv.query_one(RichLog)

        conv._maybe_write_header("user", "you ", "bold", "dim")
        conv._maybe_write_header("reyn", "reyn", "bold", "dim")
        await pilot.pause()
        # Force trim to push every anchor out of range
        log._start_line = 999_999

        # Must not raise
        conv.jump_prev_turn()
        conv.jump_next_turn()
        await pilot.pause()
        # And the one-shot warning latched
        assert conv._trim_warned


@pytest.mark.asyncio
async def test_clear_resets_trim_warn_latch() -> None:
    """``/clear`` (or Ctrl+L) lets the trim warning fire again next session."""
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        log = conv.query_one(RichLog)
        conv._maybe_write_header("user", "you ", "bold", "dim")
        await pilot.pause()
        log._start_line = 999_999
        conv.jump_prev_turn()
        assert conv._trim_warned

        conv.clear()
        assert not conv._trim_warned, "clear() must reset the trim-warned latch"


@pytest.mark.asyncio
async def test_richlog_max_lines_is_at_least_20000() -> None:
    """Pin the bumped buffer size — guards against accidental regression to 5000."""
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        log = app.query_one("#log", RichLog)
        assert log.max_lines is not None and log.max_lines >= 20_000, (
            f"RichLog max_lines should be ≥ 20000, got {log.max_lines}"
        )
