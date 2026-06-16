"""Tier 2: message bodies render inline with the header — Claude Code style.

#646 design intent: ``HH:MM > message text`` on the same logical line with
wrap continuations landing at col 8 (= ``_BODY_INDENT_WITH_TS``) to nest
visually under the body text, not the symbol.

With timestamps shown (default):
  - User  header prefix: ``HH:MM > `` (8 cells → body inline at col 8)
  - Agent header prefix: ``HH:MM ⏺ `` (8 cells → body inline at col 8)
  - First body line: inline with the header on the same RichLog line.
  - Wrap continuations: col 8 via Padding.

With timestamps hidden (F9 toggle):
  - Header prefix is ``> `` (2 cells).
  - First body line inline at col 2.
  - Wrap continuations at col 2.

These tests inspect the rendered RichLog Strips (= what the user
actually sees on screen) for inline layout, wrap continuation indent,
and the invariant that the header line is never indented.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from textual.widgets import RichLog

from reyn.chat.outbox import OutboxMessage
from reyn.interfaces.tui.app import ReynTUIApp
from reyn.interfaces.tui.widgets import ConversationView
from reyn.interfaces.tui.widgets.conversation import (
    _BODY_INDENT_NO_TS,
    _BODY_INDENT_WITH_TS,
    _GLYPH_USER,
)


def _make_app() -> ReynTUIApp:
    return ReynTUIApp(
        registry=None,
        agent_name="test-agent",
        model="test-model",
        budget_tracker=None,
    )


def _line_text(log: RichLog, index: int) -> str:
    """Plain-text projection of a single RichLog line."""
    strip = log.lines[index]
    return "".join(seg.text for seg in strip)


def _find_first_line_containing(log: RichLog, needle: str) -> int:
    for i, strip in enumerate(log.lines):
        text = "".join(seg.text for seg in strip)
        if needle in text:
            return i
    raise AssertionError(f"text {needle!r} never appeared in RichLog")


# ── inline header + body (the new Claude Code-style layout) ──────────────────


@pytest.mark.asyncio
async def test_user_message_body_is_inline_with_header() -> None:
    """Tier 2b: ``render_user_message`` puts body inline with the header (ts on).

    #646 design: the header prefix (``HH:MM > ``) and body appear on the
    same logical line.  The line found in the RichLog for "hello world"
    must start with an HH:MM timestamp (col 0, no leading spaces) and
    contain the body text — not start with col-8 spaces as in the old
    2-line layout.
    """
    import re
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        log = conv.query_one(RichLog)

        # Default state: timestamps on.
        conv._show_timestamps = True
        conv.render_user_message("hello world")
        await pilot.pause()

        idx = _find_first_line_containing(log, "hello world")
        line = _line_text(log, idx)
        # Inline: the body-containing line must start with HH:MM (col 0, no indent).
        assert re.search(r"^\d{2}:\d{2}", line), (
            f"ts-on inline line must start with HH:MM at col 0; got {line!r}"
        )
        assert _GLYPH_USER in line, f"user symbol must be on the same line; got {line!r}"
        assert "hello world" in line, f"body text must be inline; got {line!r}"
        # Must NOT start with spaces (= old 2-line indented body).
        assert not line.startswith(" "), (
            f"inline line must NOT start with spaces (col 0 anchor); got {line!r}"
        )


@pytest.mark.asyncio
async def test_user_message_body_is_inline_ts_off() -> None:
    """Tier 2b: ``render_user_message`` with ts off puts body inline at col 0.

    When timestamps are hidden, header prefix is ``> `` (2 cells).
    The first body line is inline and starts with ``>`` at col 0.
    """
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        log = conv.query_one(RichLog)

        conv._show_timestamps = False
        conv.render_user_message("hello ts-off")
        await pilot.pause()

        idx = _find_first_line_containing(log, "hello ts-off")
        line = _line_text(log, idx)
        # ts-off: line starts with symbol at col 0.
        assert line.startswith(_GLYPH_USER), (
            f"ts-off inline line must start with user symbol; got {line!r}"
        )
        assert "hello ts-off" in line, f"body text must be inline; got {line!r}"
        # Must NOT start with 2-space indent (= old 2-line layout).
        assert not line.startswith("  "), (
            f"ts-off inline line must NOT start with spaces; got {line!r}"
        )


@pytest.mark.asyncio
async def test_agent_markdown_body_is_inline_with_header() -> None:
    """Tier 2b: Agent markdown first line is inline with the speaker symbol."""
    import re
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        log = conv.query_one(RichLog)

        conv._show_timestamps = True
        msg = OutboxMessage(kind="agent", text="this is the agent body line")
        conv.render_message(msg)
        await pilot.pause()

        idx = _find_first_line_containing(log, "this is the agent body line")
        line = _line_text(log, idx)
        # Inline: body line starts with HH:MM at col 0 (not with spaces).
        assert re.search(r"^\d{2}:\d{2}", line), (
            f"agent inline line must start with HH:MM; got {line!r}"
        )
        assert not line.startswith(" "), (
            f"agent inline line must NOT start with spaces; got {line!r}"
        )


@pytest.mark.asyncio
async def test_system_message_body_is_indented() -> None:
    """Tier 2b: ``/slash`` output rendered as system kind still gets indented.

    System messages (= slash-command output) use the legacy 2-line path
    (``_maybe_write_header`` + ``_write_body``).  They are multi-line text
    blocks and are not subject to the #646 inline fix.  Body lines must
    start at col ``_BODY_INDENT_WITH_TS`` (8).
    """
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        log = conv.query_one(RichLog)

        conv._show_timestamps = True
        msg = OutboxMessage(kind="system", text="line a\nline b\nline c")
        conv.render_message(msg)
        await pilot.pause()

        for needle in ("line a", "line b", "line c"):
            idx = _find_first_line_containing(log, needle)
            body = _line_text(log, idx)
            assert body.startswith(" " * _BODY_INDENT_WITH_TS), (
                f"system line {needle!r} not indented; got {body!r}"
            )


# ── header line is never indented ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_header_line_is_not_indented() -> None:
    """Tier 2b: The inline header+body line starts at column 0 (no leading spaces).

    With the inline layout the line containing the user symbol and body
    text is also the header line — it must start at col 0 so it is
    visually distinct from any hanging-indent wrap continuations.
    """
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        log = conv.query_one(RichLog)

        conv._show_timestamps = True
        conv.render_user_message("payload")
        await pilot.pause()

        # The inline header+body line contains the ``>`` user symbol.
        # With ts on the line is ``HH:MM > payload`` — starts at col 0.
        header_idx = _find_first_line_containing(log, ">")
        header = _line_text(log, header_idx)
        assert not header.startswith(" "), (
            f"header+body inline line must NOT be indented (col-0 anchor); got {header!r}"
        )


# ── wrap continuation indented (load-bearing invariant) ──────────────────────


@pytest.mark.asyncio
async def test_long_user_line_wrap_continuation_is_indented() -> None:
    """Tier 2b: Wrap continuation of a long body line lands at the indent column.

    The first body line is inline with the header at col 0.  When the
    body wraps, the continuation must land at ``_BODY_INDENT_WITH_TS``
    (= col 8) so it nests under the body text, not back at col 0 which
    would be visually indistinguishable from a new speaker header.
    """
    app = _make_app()
    # Narrow terminal forces a wrap on a moderate-length payload.
    async with app.run_test(headless=True, size=(40, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        log = conv.query_one(RichLog)

        conv._show_timestamps = True
        # ~60 chars — well past the 40-cell terminal body width so it wraps.
        long_payload = "abcdefghij" * 6
        conv.render_user_message(long_payload)
        await pilot.pause()

        # The first line containing the payload start is the inline header line.
        first_body_idx = _find_first_line_containing(log, "abcdefghij")
        first_body = _line_text(log, first_body_idx)
        # First line: inline — starts with HH:MM (no leading space).
        assert not first_body.startswith(" "), (
            f"first (inline) line must start at col 0; got {first_body!r}"
        )

        # If wrap fired, the next non-empty line is a continuation at indent.
        if first_body_idx + 1 < len(log.lines):
            cont = _line_text(log, first_body_idx + 1)
            stripped = cont.strip()
            if stripped:  # non-empty continuation line
                assert cont.startswith(" " * _BODY_INDENT_WITH_TS), (
                    f"wrap continuation must be at col {_BODY_INDENT_WITH_TS}; got {cont!r}"
                )


# ── constant invariants ──────────────────────────────────────────────────────


def test_body_indent_constants_have_correct_values() -> None:
    """Tier 2b: ``_BODY_INDENT_WITH_TS`` and ``_BODY_INDENT_NO_TS`` match the spec.

    ts-on layout:  ``HH:MM <sym> `` = 5 (ts) + 1 (space) + 1 (sym) + 1 (space)
                   → body inline starting at col 8; wrap continuation at col 8.
    ts-off layout: ``<sym> `` = 1 (sym) + 1 (space) → body at col 2.
    """
    assert _BODY_INDENT_WITH_TS == 8, f"ts-on indent should be 8, got {_BODY_INDENT_WITH_TS}"
    assert _BODY_INDENT_NO_TS == 2, f"ts-off indent should be 2, got {_BODY_INDENT_NO_TS}"
