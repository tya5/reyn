"""Tier 2: message bodies render at the hanging-indent column.

Before this fix, ``RichLog(wrap=True)`` wrapped long lines without any
leading indent. A continuation of a long URL or code line started at
column 0 — the same column as a new ``HH:MM …`` header — so the eye
could not tell them apart. The fix wraps every body write site in
``Padding(.., (0, 0, 0, _BODY_INDENT_COLS))`` (= 7-cell left indent)
so both the first body line AND its wrapped continuation sit under the
speaker name, leaving the header anchored at column 0.

These tests inspect the rendered RichLog Strips (= what the user
actually sees on screen) for body lines vs header lines, so a future
refactor that drops the indent or accidentally applies it to headers
would surface immediately.
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
from reyn.chat.tui.app import ReynTUIApp
from reyn.chat.tui.widgets import ConversationView
from reyn.chat.tui.widgets.conversation import _BODY_INDENT_COLS


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


# ── body indent applied to common write paths ────────────────────────────────


@pytest.mark.asyncio
async def test_user_message_body_is_indented() -> None:
    """Tier 2b: ``render_user_message`` writes its body at the hanging-indent column.

    The header (``HH:MM  you  ───``) stays at column 0; the body line
    "hello world" starts at column 7 so a wrap continuation visually
    nests under the name column.
    """
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        log = conv.query_one(RichLog)

        conv.render_user_message("hello world")
        await pilot.pause()

        idx = _find_first_line_containing(log, "hello world")
        body = _line_text(log, idx)
        assert body.startswith(" " * _BODY_INDENT_COLS), (
            f"user body must start at indent col {_BODY_INDENT_COLS}; got {body!r}"
        )
        assert "hello world" in body


@pytest.mark.asyncio
async def test_agent_markdown_body_is_indented() -> None:
    """Tier 2b: Agent markdown turns render their content at the indent column."""
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        log = conv.query_one(RichLog)

        msg = OutboxMessage(kind="agent", text="this is the agent body line")
        conv.render_message(msg)
        await pilot.pause()

        idx = _find_first_line_containing(log, "this is the agent body line")
        body = _line_text(log, idx)
        assert body.startswith(" " * _BODY_INDENT_COLS), (
            f"agent body must start at indent col {_BODY_INDENT_COLS}; got {body!r}"
        )


@pytest.mark.asyncio
async def test_system_message_body_is_indented() -> None:
    """Tier 2b: ``/slash`` output rendered as system kind also gets indented."""
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        log = conv.query_one(RichLog)

        msg = OutboxMessage(kind="system", text="line a\nline b\nline c")
        conv.render_message(msg)
        await pilot.pause()

        for needle in ("line a", "line b", "line c"):
            idx = _find_first_line_containing(log, needle)
            body = _line_text(log, idx)
            assert body.startswith(" " * _BODY_INDENT_COLS), (
                f"system line {needle!r} not indented; got {body!r}"
            )


# ── header lines stay at column 0 ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_header_line_is_not_indented() -> None:
    """Tier 2b: The timestamp + speaker label header stays at column 0.

    This is the load-bearing distinction the fix delivers: header at
    column 0, body at column ``_BODY_INDENT_COLS``. Without it the wrap
    continuation of a body line and the start of a new header become
    visually identical.
    """
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        log = conv.query_one(RichLog)

        conv.render_user_message("payload")
        await pilot.pause()

        # The header line is the one that has the dash rule (── chars).
        # Find it and assert it starts at column 0.
        header_idx = _find_first_line_containing(log, "─")
        header = _line_text(log, header_idx)
        assert not header.startswith(" "), (
            f"header line must NOT be indented (column-0 anchor); got {header!r}"
        )


# ── wrap continuation also indented (the user-visible bug) ───────────────────


@pytest.mark.asyncio
async def test_long_user_line_wrap_continuation_is_indented() -> None:
    """Tier 2b: Wrap continuation of a long body line lands at the indent column.

    This is the user-visible behaviour the agent's UX audit flagged:
    a long URL or code line that wraps used to put the continuation at
    column 0, indistinguishable from a new header.
    """
    app = _make_app()
    # Narrow terminal forces a wrap on a moderate-length payload.
    async with app.run_test(headless=True, size=(40, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        log = conv.query_one(RichLog)

        # ~60 chars — well past the 40-cell terminal so it wraps.
        long_payload = "abcdefghij" * 6
        conv.render_user_message(long_payload)
        await pilot.pause()

        # First body line should be the one containing the start of payload.
        first_body_idx = _find_first_line_containing(log, "abcdefghij")
        first_body = _line_text(log, first_body_idx)
        assert first_body.startswith(" " * _BODY_INDENT_COLS), first_body

        # If wrap fired, the next line should also lead with the indent.
        if first_body_idx + 1 < len(log.lines):
            cont = _line_text(log, first_body_idx + 1)
            stripped = cont.strip()
            if stripped:  # non-empty continuation
                assert cont.startswith(" " * _BODY_INDENT_COLS), (
                    f"wrap continuation must also be indented; got {cont!r}"
                )


# ── constant invariant ───────────────────────────────────────────────────────


def test_body_indent_constant_matches_header_name_column() -> None:
    """Tier 2b: ``_BODY_INDENT_COLS`` aligns with where the name column starts.

    Header layout: ``HH:MM`` (5) + 2-space gap = 7 cells before the
    name. The body indent must match so wrapped content nests under
    the name. If either constant moves, the other must too.
    """
    assert _BODY_INDENT_COLS == 7
