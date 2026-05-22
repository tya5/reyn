"""Tier 2: cancelled stream renders distinctly from a complete reply (F-F7).

Wave-9 Topic F finding F7 (P1): the old cancel path committed the
partial accumulated text via ``end_stream`` →
``_write_agent_markdown_with_fold``, applying the SAME Markdown
styling used for complete replies. Then a separate
``"  ⌁ cancelled"`` suffix line was appended. The user couldn't
tell a cancelled fragment from a finished reply at a glance —
Markdown rendering applied bold / headers / code blocks to the
partial (often wrong, since the fragment had half-closed fences),
and the dim suffix sat below the viewport when the partial filled
the screen.

The new ``end_stream_cancelled`` path:
  - writes a bold dim-red ``✗ cancelled (partial reply):`` HEADER
    BEFORE the partial body, where the user's eye lands first
  - renders the partial body as plain dim italic text (no Markdown)
    so half-closed fences don't produce misleading styling
  - normal completion path (``end_stream``) is unchanged

Public surfaces tested:
  - cancelled stream produces both the header marker and the dim
    body text in the conversation log
  - the partial body does NOT get Markdown rendering (= no inline
    Markdown widget mounted after cancel)
  - return value still carries the partial text (= callers like
    ``action_cancel_inflight`` use this to detect "did any stream
    cancel" for the summary line)
  - normal ``end_stream`` (= non-cancelled finalization) is
    untouched (regression guard)
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def _richlog_plain(conv) -> str:
    """Collect the plain text of every line in the RichLog."""
    log = conv._log()
    # ``log.lines`` is the list of rendered Strip objects; each has a
    # ``.text`` property carrying the plain text of that row.
    lines = []
    for strip in getattr(log, "lines", []):
        text = getattr(strip, "text", "")
        if text:
            lines.append(text)
    return "\n".join(lines)


@pytest.mark.asyncio
async def test_end_stream_cancelled_emits_header_and_dim_body() -> None:
    """Tier 2: cancelled stream commits header + partial body to RichLog."""
    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets import ConversationView

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        row = conv.begin_stream("cancel-test-id", "test-agent")
        row.append("# Half-written\n\nThis is a partial reply that ")
        row.append("the user is about to cancel.")
        await pilot.pause()

        returned = conv.end_stream_cancelled("cancel-test-id")
        await pilot.pause()

        # Return value carries the partial text for the caller.
        assert "partial reply" in returned
        assert "Half-written" in returned

        log_text = _richlog_plain(conv)
        # Header marker visible at the top of the fragment.
        assert "✗ cancelled (partial reply):" in log_text, (
            f"header marker missing from log; got:\n{log_text!r}"
        )
        # Partial body text present.
        assert "Half-written" in log_text
        assert "This is a partial reply" in log_text


@pytest.mark.asyncio
async def test_end_stream_cancelled_does_not_render_markdown() -> None:
    """Tier 2: cancelled partial does NOT route through the Markdown renderer.

    Half-closed fences / unclosed bold / broken lists would render
    incorrectly under Markdown. The dim plain text reads as "fragment"
    without misleading styling.
    """
    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets import ConversationView

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        row = conv.begin_stream("md-cancel-id", "test-agent")
        # Half-opened code fence — Markdown would render this badly.
        row.append("Here is some code:\n```python\ndef foo():")
        await pilot.pause()

        conv.end_stream_cancelled("md-cancel-id")
        await pilot.pause()

        log_text = _richlog_plain(conv)
        # Raw fragment text should be present verbatim — fence markers
        # included — because we skipped Markdown rendering.
        assert "```python" in log_text or "def foo()" in log_text, (
            f"partial fence text should appear in raw form, got:\n{log_text!r}"
        )


@pytest.mark.asyncio
async def test_end_stream_unchanged_for_completed_reply() -> None:
    """Tier 2: normal completion still commits with Markdown styling.

    Regression guard: only the cancel path was changed. A successful
    finish must continue to produce a real Markdown render with no
    "cancelled (partial reply)" header.
    """
    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets import ConversationView

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        row = conv.begin_stream("normal-end-id", "test-agent")
        row.append("This is a complete reply.")
        await pilot.pause()

        conv.end_stream("normal-end-id")  # NOT end_stream_cancelled
        await pilot.pause()

        log_text = _richlog_plain(conv)
        # The cancellation header must NOT appear.
        assert "cancelled (partial reply)" not in log_text, (
            f"normal end_stream should not emit cancel header, got:\n{log_text!r}"
        )
        # The reply text itself is present.
        assert "complete reply" in log_text
