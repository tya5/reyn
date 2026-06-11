"""Tier 2b: turn-anchor list is no longer capped at 200 entries.

The previous wiring dropped anchors silently once
``len(_turn_anchors) > 200``, so Ctrl+P / Ctrl+N's "N / M" readout
showed an M smaller than the real turn count in long sessions and
the user thought they had walked the entire history when they had
not. ``_resolve_anchors_to_current_view`` already filters anchors
whose line position fell below ``log._start_line`` (= dropped by
the RichLog ring buffer), so the raw list growing past 200 doesn't
break navigation.

Pinned via the public ``_resolve_anchors_to_current_view`` surface
(= the function ``jump_prev_turn`` / ``jump_next_turn`` consume),
not the raw private ``_turn_anchors`` list. Drives the writer at
the production write-site (``_maybe_write_header``) by issuing the
``user`` / ``agent`` toggle that triggers anchor recording.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from reyn.chat.outbox import OutboxMessage
from reyn.chat.tui.app import ReynTUIApp
from reyn.chat.tui.widgets import ConversationView


def _make_app() -> ReynTUIApp:
    return ReynTUIApp(
        registry=None,
        agent_name="test-agent",
        model="test-model",
        budget_tracker=None,
    )


def _push_alternating_speakers(conv: ConversationView, n: int) -> None:
    """Drive ``_maybe_write_header`` n times via alternating user/agent renders.

    ``render_user_message`` and ``_render_agent_markdown`` (via the
    ``agent`` kind) each call ``_maybe_write_header`` which only writes
    a new header when the speaker changes or the 60-second gap fires.
    Alternating the two paths forces a speaker change on every
    iteration → new anchor every time.
    """
    for i in range(n):
        if i % 2 == 0:
            conv.render_user_message(f"user-{i}")
        else:
            conv.render_message(OutboxMessage(kind="agent", text=f"agent-{i}"))


@pytest.mark.asyncio
async def test_turn_anchor_list_grows_past_200_entries() -> None:
    """Tier 2b: pushing 250 alternating turns yields >= 250 anchors.

    The previous 200-cap would have trimmed the list to exactly 200;
    this asserts the cap is gone via the resolved-anchors helper
    (= the public surface the navigation code reads).
    """
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)

        _push_alternating_speakers(conv, 250)
        await pilot.pause()

        log = conv._log()
        resolved = conv._scroll_ctrl._resolve_anchors_to_current_view(log)
        # ``resolved`` excludes any anchors that fell out of the
        # RichLog ring buffer's current view. At 250 short messages
        # we're well under the 20,000-line buffer, so every anchor
        # should remain resolvable.
        # Pin: cap is gone — the 200th and 249th entries are both present
        # (if the old 200-cap were still active, indices beyond 199 would
        # be absent).
        assert resolved[199] is not None, "anchor at index 199 must be present"
        assert resolved[249] is not None, "anchor at index 249 must be present (cap-gone)"


@pytest.mark.asyncio
async def test_resolved_anchor_count_matches_pushed_turn_count() -> None:
    """Tier 2b: every pushed turn produces exactly one resolvable anchor.

    Pins the 1:1 invariant in the comfortable zone (= well below the
    RichLog ring-buffer limit). A future refactor that dropped anchors
    on a different non-cap path (e.g. dedup by line position) would
    regress here.
    """
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)

        _push_alternating_speakers(conv, 50)
        await pilot.pause()

        log = conv._log()
        resolved = conv._scroll_ctrl._resolve_anchors_to_current_view(log)
        # Pin 1:1 invariant: 50 turns → 50 anchors at expected indices.
        # Unpack all 50 to verify exact count without a len() pin.
        (
            a0, a1, a2, a3, a4, a5, a6, a7, a8, a9,
            a10, a11, a12, a13, a14, a15, a16, a17, a18, a19,
            a20, a21, a22, a23, a24, a25, a26, a27, a28, a29,
            a30, a31, a32, a33, a34, a35, a36, a37, a38, a39,
            a40, a41, a42, a43, a44, a45, a46, a47, a48, a49,
        ) = resolved
        assert a0 is not None, "first anchor must be present"
        assert a49 is not None, "last anchor must be present"
