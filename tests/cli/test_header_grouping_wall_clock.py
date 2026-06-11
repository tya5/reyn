"""Tier 2: header grouping uses wall clock, aligning with displayed timestamp (G-F7).

Wave-10 follow-up Topic G finding F7 (P3): the 60-second grouping
window in ``_maybe_write_header`` used ``time.monotonic()`` while
the visible header timestamp uses ``time.strftime`` (= wall clock).
The two clocks diverge across system sleep on platforms where
``CLOCK_MONOTONIC`` doesn't advance during suspend (= classic
behavior on most Linux + older macOS). After a sleep/wake, two
messages displayed with wall-clock timestamps an hour apart can
share a grouping bucket simply because the monotonic delta is
~0s. The user sees the timestamps drift while the header
grouping stays inconsistent.

After the fix grouping uses ``time.time()`` (= same clock as the
displayed timestamp), so the grouping decision and the visible
timestamp stay in lockstep.

Public surfaces tested:
  - ``_last_speaker_at`` post-header equals roughly ``time.time()``
    (= the wall-clock reading), not ``time.monotonic()``
  - same-speaker within window suppresses second header
    (regression guard)
  - same-speaker after the 60s window emits a new header
    (regression guard, tested via back-dating ``_last_speaker_at``)
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


@pytest.mark.asyncio
async def test_last_speaker_at_is_wall_clock_after_header_write() -> None:
    """Tier 2: ``_last_speaker_at`` should be a wall-clock value.

    Wall clock is in the range of ``time.time()`` (= seconds since
    epoch, ~1.78e9 in 2026). Monotonic is platform-dependent but
    typically a much smaller number (seconds since boot or since
    some other arbitrary reference). A simple range check
    distinguishes the two.
    """
    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets import ConversationView

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        conv.render_user_message("first")
        await pilot.pause()
        now_wall = time.time()
        # The stored timestamp should be within ~10s of the wall-clock
        # reading and definitely in the wall-clock range (= > 1e9).
        assert conv.last_speaker_at > 1e9, (
            f"last_speaker_at should be a wall-clock timestamp "
            f"(seconds since epoch); got {conv.last_speaker_at}"
        )
        assert abs(conv.last_speaker_at - now_wall) < 10.0, (
            f"last_speaker_at should be roughly now (within 10s); "
            f"got {conv.last_speaker_at} vs now={now_wall}"
        )


@pytest.mark.asyncio
async def test_same_speaker_within_window_groups_under_one_header() -> None:
    """Tier 2b: same-speaker burst within 60s shares header (regression guard)."""
    from reyn.chat.outbox import OutboxMessage
    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets import ConversationView

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        conv._render_agent_markdown(OutboxMessage(kind="agent", text="r1"))
        await pilot.pause()
        anchors_after_first = len(conv._scroll_ctrl._turn_anchors)
        # Within 60s — second same-speaker message must NOT emit a new
        # header (= grouped under the first).
        conv._render_agent_markdown(OutboxMessage(kind="agent", text="r2"))
        await pilot.pause()
        anchors_after_second = len(conv._scroll_ctrl._turn_anchors)
        assert anchors_after_second == anchors_after_first, (
            f"within-window same-speaker should group; "
            f"anchors before={anchors_after_first} after={anchors_after_second}"
        )


@pytest.mark.asyncio
async def test_same_speaker_past_window_emits_new_header() -> None:
    """Tier 2b: same-speaker past 60s emits a new header (regression guard).

    Simulated by back-dating ``_last_speaker_at``.
    """
    from reyn.chat.outbox import OutboxMessage
    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets import ConversationView

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        conv._render_agent_markdown(OutboxMessage(kind="agent", text="r1"))
        await pilot.pause()
        anchors_before = len(conv._scroll_ctrl._turn_anchors)
        # Back-date the stored timestamp 120s into the past so the
        # next same-speaker message is past the 60s window.
        conv._renderer._last_speaker_at = time.time() - 120.0
        conv._render_agent_markdown(OutboxMessage(kind="agent", text="r2"))
        await pilot.pause()
        anchors_after = len(conv._scroll_ctrl._turn_anchors)
        assert anchors_after == anchors_before + 1, (
            f"past-window same-speaker should emit new header (+1 anchor); "
            f"got before={anchors_before} after={anchors_after}"
        )
