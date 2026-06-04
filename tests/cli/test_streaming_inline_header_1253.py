"""Tier 2: streaming agent replies commit inline ``HH:MM ⏺ <first-line>`` header (#1253 Plan A).

Design: ``end_stream`` now calls ``_commit_stream_inline`` which writes the
reply in the same form as the non-streaming ``_render_agent_markdown`` path
(inline ``HH:MM ⏺ <first-line>``), with grouping decision captured at
``begin_stream`` time (guardrail ①).

Three invariants tested:

1. **Inline commit**: after begin/append/end_stream the committed RichLog line
   that contains the reply body starts with ``HH:MM ⏺`` at col 0 (not a
   separate lone-glyph header line above an indented body).

2. **Grouping regression (guardrail ①)**: two consecutive streamed replies
   within the grouping window — the second reply's first line is body-at-indent
   (no fresh ``⏺`` header glyph on the same line as the body text).

3. **/copy preservation**: after a multi-line streamed reply,
   ``conv.last_reply_text()`` returns the FULL reply including the first line.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from textual.widgets import RichLog

from reyn.chat.tui.app import ReynTUIApp
from reyn.chat.tui.widgets import ConversationView
from reyn.chat.tui.widgets.conversation import _GLYPH_AGENT


def _make_app() -> ReynTUIApp:
    return ReynTUIApp(
        registry=None,
        agent_name="test-agent",
        model="test-model",
        budget_tracker=None,
    )


def _richlog_lines(conv: ConversationView) -> list[str]:
    """Plain-text list of every non-empty line in the RichLog."""
    log = conv._log()
    return [
        getattr(strip, "text", "") or ""
        for strip in getattr(log, "lines", [])
    ]


def _find_first_line_containing(lines: list[str], needle: str) -> int:
    for i, text in enumerate(lines):
        if needle in text:
            return i
    raise AssertionError(f"text {needle!r} never appeared in RichLog lines: {lines!r}")


@pytest.mark.asyncio
async def test_streaming_end_commits_inline_header() -> None:
    """Tier 2: end_stream writes ``HH:MM ⏺ <body>`` inline, not a lone glyph above indented body.

    Mirrors the assertion style of
    ``test_body_hanging_indent.py::test_agent_markdown_body_is_inline_with_header``
    but via the streaming path.
    """
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)

        conv._show_timestamps = True
        row = conv.begin_stream("msg-inline-test", "test-agent")
        row.append("hello world line one")
        await pilot.pause()

        conv.end_stream("msg-inline-test")
        await pilot.pause()

        lines = _richlog_lines(conv)
        idx = _find_first_line_containing(lines, "hello world line one")
        line = lines[idx]

        # Inline: the body-containing line must start with HH:MM at col 0.
        assert re.search(r"^\d{2}:\d{2}", line), (
            f"streaming inline line must start with HH:MM at col 0; got {line!r}"
        )
        # The agent glyph must be on the SAME line as the body text.
        assert _GLYPH_AGENT in line, (
            f"agent glyph must be on the same inline line; got {line!r}"
        )
        # Must NOT start with spaces (= old indented body on a separate line).
        assert not line.startswith(" "), (
            f"inline line must NOT start with spaces (col-0 anchor); got {line!r}"
        )

        # Verify no standalone lone-glyph line ABOVE the body line.
        # A lone-glyph line would be a line containing only the timestamp + glyph
        # (i.e. the glyph is present but the body text is NOT on that line).
        for i, text in enumerate(lines[:idx]):
            if _GLYPH_AGENT in text and "hello world" not in text:
                # This would be the old-style standalone header above the body.
                assert False, (
                    f"found a standalone glyph line at [{i}] above body line [{idx}]: "
                    f"{text!r}\nbody line: {line!r}"
                )


@pytest.mark.asyncio
async def test_streaming_grouping_captured_at_start() -> None:
    """Tier 2: second consecutive stream within window groups (guardrail ①).

    Two back-to-back begin/append/end_stream calls within the grouping window.
    The first reply must have the ``HH:MM ⏺`` header inline.
    The second reply must NOT have a fresh ``⏺`` glyph on its first body line
    (it should be grouped = body-at-indent only, no new header).
    """
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)

        conv._show_timestamps = True

        # First stream.
        row1 = conv.begin_stream("msg-group-1", "test-agent")
        row1.append("first reply body text")
        await pilot.pause()
        conv.end_stream("msg-group-1")
        await pilot.pause()

        # Second stream immediately after (within grouping window).
        row2 = conv.begin_stream("msg-group-2", "test-agent")
        row2.append("second reply body text")
        await pilot.pause()
        conv.end_stream("msg-group-2")
        await pilot.pause()

        lines = _richlog_lines(conv)

        # First reply: must have inline HH:MM ⏺ on the same line as the body.
        idx1 = _find_first_line_containing(lines, "first reply body text")
        line1 = lines[idx1]
        assert re.search(r"^\d{2}:\d{2}", line1), (
            f"first reply must have inline header; got {line1!r}"
        )
        assert _GLYPH_AGENT in line1, (
            f"first reply inline line must have glyph; got {line1!r}"
        )

        # Second reply: body line should NOT start with HH:MM ⏺ (grouped = body-only).
        idx2 = _find_first_line_containing(lines, "second reply body text")
        line2 = lines[idx2]
        # Grouped: the body line must start with spaces (= hanging-indent Padding),
        # NOT with HH:MM or the glyph.
        assert not re.search(r"^\d{2}:\d{2}", line2), (
            f"second grouped reply must NOT have a fresh HH:MM header; got {line2!r}"
        )
        assert _GLYPH_AGENT not in line2, (
            f"second grouped reply body line must NOT contain glyph (grouped); got {line2!r}"
        )


@pytest.mark.asyncio
async def test_streaming_copy_preserves_full_reply() -> None:
    """Tier 2: last_reply_text() returns the full multi-line reply after end_stream.

    Guards the /copy ring-buffer fix: ``_commit_stream_inline`` records
    the full reply (including the first line), NOT just the body rest.
    If only the rest were recorded, the first line would be missing from
    the /copy result.
    """
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)

        full_text = "First line of reply\nSecond line of reply\nThird line of reply"
        row = conv.begin_stream("msg-copy-test", "test-agent")
        row.append(full_text)
        await pilot.pause()

        conv.end_stream("msg-copy-test")
        await pilot.pause()

        result = conv.last_reply_text()
        assert result is not None, "last_reply_text() must be non-None after end_stream"
        # Full reply including first line must be present.
        assert "First line of reply" in result, (
            f"last_reply_text() must include first line; got {result!r}"
        )
        assert "Second line of reply" in result, (
            f"last_reply_text() must include second line; got {result!r}"
        )
        assert "Third line of reply" in result, (
            f"last_reply_text() must include third line; got {result!r}"
        )
        # The full text should match exactly (guardrail: not just a subset).
        assert result == full_text, (
            f"last_reply_text() must equal the full reply text;\n"
            f"expected: {full_text!r}\ngot:      {result!r}"
        )
