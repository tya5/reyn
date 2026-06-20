"""Tier 2: the cancel-inflight stream-seal loop uses the public stream_rows accessor.

Regression for a live TUI crash found in real-terminal dogfood (2026-06-20):
pressing Ctrl+C to cancel an in-flight skill/stream raised

    AttributeError: 'ConversationView' object has no attribute '_stream_rows'

at ``app.action_cancel_inflight`` — it iterated ``conv._stream_rows`` directly,
but the ``tui-pr4`` refactor moved that private dict onto the extracted
``_StreamController``. ConversationView exposes the rows via the public
``stream_rows`` property (a snapshot copy); the cancel path must use it.

This test reproduces the EXACT operation the cancel handler performs against a
ConversationView with a live stream row — iterate ``stream_rows.keys()`` and
``end_stream_cancelled`` each — and asserts it seals the row without raising.

Falsification: if the public ``stream_rows`` accessor is removed/renamed (or a
caller reverts to the moved private ``_stream_rows``), the keys() access here
raises AttributeError exactly as the live crash did.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


@pytest.mark.asyncio
async def test_cancel_seal_loop_uses_public_stream_rows_accessor() -> None:
    """Tier 2: the cancel handler's seal loop runs against the public accessor."""
    from reyn.interfaces.tui.app import ReynTUIApp
    from reyn.interfaces.tui.widgets import ConversationView

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)

        # A live in-flight stream (what exists when the user hits Ctrl+C mid-reply).
        row = conv.begin_stream("seal-test-id", "test-agent")
        row.append("partial reply the user is about to cancel")
        await pilot.pause()

        # The public accessor must expose the live row (this is what the crash
        # path now reads instead of the moved private ``_stream_rows``).
        assert "seal-test-id" in conv.stream_rows, (
            "expected the live stream row via the public stream_rows accessor"
        )

        # Reproduce the EXACT operation app.action_cancel_inflight performs.
        # Pre-fix this raised AttributeError on ``conv._stream_rows``.
        cancelled = 0
        for msg_id in list(conv.stream_rows.keys()):
            conv.end_stream_cancelled(msg_id)
            cancelled += 1
        await pilot.pause()

        assert cancelled == 1, "expected the one live stream row to be sealed"
        assert "seal-test-id" not in conv.stream_rows, (
            "the sealed row should no longer be in the live registry"
        )


@pytest.mark.asyncio
async def test_stream_rows_is_snapshot_copy() -> None:
    """Tier 2: stream_rows returns a snapshot — mutating it cannot corrupt state.

    The cancel loop wraps the accessor in ``list(...)`` because it mutates the
    underlying registry via ``end_stream_cancelled`` while iterating. The
    property returning a copy is what makes that safe.
    """
    from reyn.interfaces.tui.app import ReynTUIApp
    from reyn.interfaces.tui.widgets import ConversationView

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        conv.begin_stream("snap-id", "test-agent")
        await pilot.pause()

        snap = conv.stream_rows
        snap.clear()  # mutate the returned dict
        # internal registry is unaffected — the live row is still tracked
        assert "snap-id" in conv.stream_rows, (
            "stream_rows must return a copy; clearing it must not drop live rows"
        )
