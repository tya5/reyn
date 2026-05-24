"""Tier 2: message bodies render at the dynamic hanging-indent column.

With timestamps shown (default), bodies start at col 8 (= ``_BODY_INDENT_WITH_TS``);
with timestamps hidden (F9 toggle), bodies start at col 2 (= ``_BODY_INDENT_NO_TS``).

Before the hanging-indent fix, ``RichLog(wrap=True)`` wrapped long lines
without any leading indent so a continuation landed at column 0 —
indistinguishable from a new speaker header. The Padding at every body
write site keeps wrap continuations visually nested under the symbol.

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
from reyn.chat.tui.widgets.conversation import _BODY_INDENT_NO_TS, _BODY_INDENT_WITH_TS


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
    """Tier 2b: ``render_user_message`` writes its body at the ts-on hanging-indent column.

    The header (``HH:MM >`` with ts on) stays at column 0; the body line
    "hello world" starts at column ``_BODY_INDENT_WITH_TS`` (8) so a wrap
    continuation visually nests under the symbol column.
    """
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        log = conv.query_one(RichLog)

        # Default state: timestamps on → indent 8.
        conv._show_timestamps = True
        conv.render_user_message("hello world")
        await pilot.pause()

        idx = _find_first_line_containing(log, "hello world")
        body = _line_text(log, idx)
        assert body.startswith(" " * _BODY_INDENT_WITH_TS), (
            f"user body (ts on) must start at indent col {_BODY_INDENT_WITH_TS}; got {body!r}"
        )
        assert "hello world" in body


@pytest.mark.asyncio
async def test_user_message_body_is_indented_ts_off() -> None:
    """Tier 2b: ``render_user_message`` with ts off uses the narrow indent (col 2)."""
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        log = conv.query_one(RichLog)

        conv._show_timestamps = False
        conv.render_user_message("hello ts-off")
        await pilot.pause()

        idx = _find_first_line_containing(log, "hello ts-off")
        body = _line_text(log, idx)
        assert body.startswith(" " * _BODY_INDENT_NO_TS), (
            f"user body (ts off) must start at indent col {_BODY_INDENT_NO_TS}; got {body!r}"
        )


@pytest.mark.asyncio
async def test_agent_markdown_body_is_indented() -> None:
    """Tier 2b: Agent markdown turns render their content at the ts-on indent column."""
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
        body = _line_text(log, idx)
        assert body.startswith(" " * _BODY_INDENT_WITH_TS), (
            f"agent body must start at indent col {_BODY_INDENT_WITH_TS}; got {body!r}"
        )


@pytest.mark.asyncio
async def test_system_message_body_is_indented() -> None:
    """Tier 2b: ``/slash`` output rendered as system kind also gets indented."""
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


# ── header lines stay at column 0 ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_header_line_is_not_indented() -> None:
    """Tier 2b: The symbol header stays at column 0.

    This is the load-bearing distinction: header at column 0,
    body at column ``_BODY_INDENT_WITH_TS``. Without it the wrap
    continuation of a body line and the start of a new header become
    visually identical.
    """
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        log = conv.query_one(RichLog)

        conv._show_timestamps = True
        conv.render_user_message("payload")
        await pilot.pause()

        # The header line contains the ``>`` user symbol.
        # With ts on the line is ``HH:MM >`` — starts at column 0 (no leading space).
        header_idx = _find_first_line_containing(log, ">")
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

        conv._show_timestamps = True
        # ~60 chars — well past the 40-cell terminal so it wraps.
        long_payload = "abcdefghij" * 6
        conv.render_user_message(long_payload)
        await pilot.pause()

        # First body line should be the one containing the start of payload.
        first_body_idx = _find_first_line_containing(log, "abcdefghij")
        first_body = _line_text(log, first_body_idx)
        assert first_body.startswith(" " * _BODY_INDENT_WITH_TS), first_body

        # If wrap fired, the next line should also lead with the indent.
        if first_body_idx + 1 < len(log.lines):
            cont = _line_text(log, first_body_idx + 1)
            stripped = cont.strip()
            if stripped:  # non-empty continuation
                assert cont.startswith(" " * _BODY_INDENT_WITH_TS), (
                    f"wrap continuation must also be indented; got {cont!r}"
                )


# ── constant invariants ──────────────────────────────────────────────────────


def test_body_indent_constants_have_correct_values() -> None:
    """Tier 2b: ``_BODY_INDENT_WITH_TS`` and ``_BODY_INDENT_NO_TS`` match the spec.

    ts-on layout:  ``HH:MM <sym>`` = 5 (ts) + 1 (space) + 1 (sym) + 1 (space)
                   → body starts col 8.
    ts-off layout: ``<sym>`` = 1 (sym) + 1 (space) → body starts col 2.
    """
    assert _BODY_INDENT_WITH_TS == 8, f"ts-on indent should be 8, got {_BODY_INDENT_WITH_TS}"
    assert _BODY_INDENT_NO_TS == 2, f"ts-off indent should be 2, got {_BODY_INDENT_NO_TS}"
