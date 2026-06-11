"""Tier 2: conv pane inline header + body — #646 Claude Code style.

Before this fix, ``render_user_message`` and ``_render_agent_markdown``
emitted TWO visible lines per turn:
  1. ``HH:MM >``          (bare header)
  2. ``        body text`` (body at col-8 indent)

#646 design intent (Claude Code style) specifies ONE line:
  ``HH:MM > body text``  — header prefix + body inline
  ``        wrap cont``  — wrap continuation at col 8 (hanging indent)

These tests verify the new inline layout via the public RichLog surface
(= ``log.lines`` Strip text) without asserting on private ``_`` state.
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

from reyn.chat.outbox import OutboxMessage
from reyn.chat.tui.app import ReynTUIApp
from reyn.chat.tui.widgets import ConversationView
from reyn.chat.tui.widgets.conversation import (
    _BODY_INDENT_NO_TS,
    _BODY_INDENT_WITH_TS,
    _GLYPH_AGENT,
    _GLYPH_USER,
    _GROUP_WINDOW_S,
)


def _make_app() -> ReynTUIApp:
    return ReynTUIApp(
        registry=None,
        agent_name="test-agent",
        model="test-model",
        budget_tracker=None,
    )


def _log_lines(log: RichLog) -> list[str]:
    """Return plain-text of every line in the RichLog."""
    return ["".join(seg.text for seg in strip) for strip in log.lines]


def _find_first_line_containing(log: RichLog, needle: str) -> str:
    """Return the first RichLog line containing ``needle``, or raise."""
    for strip in log.lines:
        text = "".join(seg.text for seg in strip)
        if needle in text:
            return text
    raise AssertionError(f"{needle!r} not found in RichLog")


# ── Test 1: render_user_message single-line inline layout ────────────────────


@pytest.mark.asyncio
async def test_render_user_message_single_line_inline() -> None:
    """Tier 2b: render_user_message produces inline header+body on one line.

    #646 fix: instead of a bare ``HH:MM >`` header line followed by a
    separately indented body line, the output must be ``HH:MM > hello``
    on a single RichLog line (no leading spaces).

    Verification: find the line containing the body text; assert it
    also contains the HH:MM timestamp and the user symbol, and does NOT
    start with spaces.
    """
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        log = conv.query_one(RichLog)

        conv._show_timestamps = True
        conv.render_user_message("hello")
        await pilot.pause()

        line = _find_first_line_containing(log, "hello")
        # Inline: the body-containing line also carries the header.
        assert re.search(r"\d{2}:\d{2}", line), (
            f"inline line must contain HH:MM timestamp; got {line!r}"
        )
        assert _GLYPH_USER in line, (
            f"inline line must contain user symbol {_GLYPH_USER!r}; got {line!r}"
        )
        assert not line.startswith(" "), (
            f"inline line must start at col 0 (no leading spaces); got {line!r}"
        )

        # Verify the old 2-line layout is gone: no separate header-only line.
        lines = _log_lines(log)
        ts_only_lines = [
            l for l in lines
            if re.search(r"^\d{2}:\d{2} " + re.escape(_GLYPH_USER) + r"\s*$", l)
        ]
        assert not ts_only_lines, (
            f"old bare header line (HH:MM > alone) must not appear; "
            f"found: {ts_only_lines}"
        )


# ── Test 2: render_user_message wrap continuation at col 8 ───────────────────


@pytest.mark.asyncio
async def test_render_user_message_wrap_continuation_at_col_8() -> None:
    """Tier 2b: long user message wraps with continuation at col 8.

    When the body text exceeds the body_width, wrap continuations must
    land at ``_BODY_INDENT_WITH_TS`` (= col 8) so they nest under the
    body text, not at col 0 where they would be indistinguishable from
    a new speaker header.

    We use a narrow terminal (40 cols) to force wrapping on a ~60-char
    payload and check the line AFTER the inline header+first-body line.
    """
    app = _make_app()
    async with app.run_test(headless=True, size=(40, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        log = conv.query_one(RichLog)

        conv._show_timestamps = True
        # Payload long enough to force wrap at 40-col terminal.
        payload = "abcdefghij" * 6  # 60 chars
        conv.render_user_message(payload)
        await pilot.pause()

        lines = _log_lines(log)
        # Find the inline header+first-body line (col 0, no leading space).
        first_idx = next(
            (i for i, l in enumerate(lines) if "abcdefghij" in l and not l.startswith(" ")),
            None,
        )
        assert first_idx is not None, (
            "inline header+first-body line (no leading space, contains payload) not found"
        )

        # If wrap fired, the next non-empty line must be the continuation.
        cont_candidates = [
            l for l in lines[first_idx + 1:]
            if l.strip()
        ]
        if cont_candidates:
            cont = cont_candidates[0]
            # Continuation must start with the indent (spaces), not col 0.
            assert cont.startswith(" " * _BODY_INDENT_WITH_TS), (
                f"wrap continuation must start at col {_BODY_INDENT_WITH_TS}; "
                f"got {cont!r}"
            )


# ── Test 3: _render_agent_markdown inlines first line with symbol ─────────────


@pytest.mark.asyncio
async def test_render_agent_markdown_inlines_first_line() -> None:
    """Tier 2b: _render_agent_markdown puts first line inline with agent symbol.

    The agent reply ``render_message(kind='agent', text='Hello')`` must
    produce a line starting with ``HH:MM ⏺`` that also contains ``Hello``
    — not a bare ``HH:MM ⏺`` header on its own line followed by a
    separately indented ``Hello``.
    """
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        log = conv.query_one(RichLog)

        conv._show_timestamps = True
        msg = OutboxMessage(kind="agent", text="Hello")
        conv.render_message(msg)
        await pilot.pause()

        line = _find_first_line_containing(log, "Hello")
        # The line containing "Hello" must also carry the agent symbol.
        assert _GLYPH_AGENT in line, (
            f"agent inline line must contain {_GLYPH_AGENT!r}; got {line!r}"
        )
        assert re.search(r"\d{2}:\d{2}", line), (
            f"agent inline line must contain HH:MM; got {line!r}"
        )
        assert not line.startswith(" "), (
            f"agent inline line must start at col 0; got {line!r}"
        )


# ── Test 4: ts toggle off — header indent col 2, body inline ─────────────────


@pytest.mark.asyncio
async def test_ts_off_inline_header_at_col_0() -> None:
    """Tier 2b: ts toggle off → header prefix is symbol only, body still inline.

    With ts hidden, header prefix = ``> `` (2 cells at col 0).
    The inline line must start with the user symbol at col 0 and contain
    the body text — no separate indented body line at col 2.
    """
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        log = conv.query_one(RichLog)

        conv._show_timestamps = False
        conv.render_user_message("body inline ts-off")
        await pilot.pause()

        line = _find_first_line_containing(log, "body inline ts-off")
        # ts-off inline: starts with symbol at col 0.
        assert line.startswith(_GLYPH_USER), (
            f"ts-off inline line must start with user symbol; got {line!r}"
        )
        assert not re.search(r"^\d{2}:\d{2}", line), (
            f"ts-off inline line must NOT start with HH:MM; got {line!r}"
        )
        assert not line.startswith(" "), (
            f"ts-off inline line must NOT start with spaces; got {line!r}"
        )

        # Old 2-line layout check: no separate bare-symbol header line.
        lines = _log_lines(log)
        bare_header = [
            l for l in lines
            if re.match(r"^" + re.escape(_GLYPH_USER) + r"\s*$", l)
        ]
        assert not bare_header, (
            f"old bare symbol header line must not appear; found: {bare_header}"
        )


# ── Test 5: speaker grouping suppresses repeat header, body still inline ──────


@pytest.mark.asyncio
async def test_speaker_grouping_suppresses_header_body_still_inline() -> None:
    """Tier 2b: grouped consecutive messages suppress the header but body is still written.

    When two user messages arrive within ``_GROUP_WINDOW_S`` seconds, only
    the first gets a header+body inline line.  The second message must
    still appear in the log (body written via ``_write_body`` at hanging
    indent) — it must not be silently dropped.

    This verifies the grouping path in ``_maybe_write_inline_header_body``:
    ``is_new_turn=False`` → ``_write_body`` branch.
    """
    import time as _time

    app = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        log = conv.query_one(RichLog)

        conv._show_timestamps = True
        # Force the same speaker within the grouping window.
        conv._renderer._last_speaker = ""  # ensure first message gets a header.
        conv.render_user_message("first message")
        await pilot.pause()

        # Second message from same speaker within window (no time has passed).
        # _last_speaker is now "you" and _last_speaker_at is current wall time.
        conv.render_user_message("second message")
        await pilot.pause()

        lines = _log_lines(log)

        # Both messages must appear in the log.
        first_lines = [l for l in lines if "first message" in l]
        second_lines = [l for l in lines if "second message" in l]
        assert first_lines, "first message must appear in log"
        assert second_lines, "second message must appear in log"

        # First message: inline with header (no leading space).
        assert not first_lines[0].startswith(" "), (
            f"first message (new turn) must be inline at col 0; got {first_lines[0]!r}"
        )

        # Second message: grouped (header suppressed) → body via _write_body
        # (leading spaces = hanging indent).
        assert second_lines[0].startswith(" "), (
            f"second message (grouped) must be at hanging indent; got {second_lines[0]!r}"
        )
