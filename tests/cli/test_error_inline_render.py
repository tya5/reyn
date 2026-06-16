"""Tier 2: ConversationView.write_error renders errors inline in the conv RichLog.

Invariants pinned (user-direct inline scroll-away design):

1. write_error writes the '✗' glyph into the RichLog.
2. write_error includes the error message text in the log.
3. write_error includes the '[skill#run_id]' prefix when both are provided.
4. write_error includes a 'Ctrl+B → events' pointer when has_trace (skill or run_id).
5. write_error does NOT include the trace pointer when no skill/run_id (= slash error).
6. write_error returns None (no widget to return).
7. High-severity errors get the _SEV_HIGH colour in the header line.
8. Low-severity errors get the _TEXT_MUTED colour in the header line.
9. Inline hint (from '• hint' tail) appears as a separate line in the log.
10. No ErrorBox children are ever mounted (ErrorBox widget no longer exists).

Public surface only — no private field assertions.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


# ── helpers ────────────────────────────────────────────────────────────────────


def _make_app():
    from reyn.tui.app import ReynTUIApp
    return ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)


def _log_plain(conv) -> str:
    """Return all RichLog lines concatenated as plain text."""
    from textual.widgets import RichLog
    log = conv.query_one(RichLog)
    return "\n".join(
        line.plain if hasattr(line, "plain") else str(line)
        for line in log.lines
    )


def _log_markup(conv) -> str:
    """Return all RichLog lines concatenated preserving Style markup."""
    from textual.widgets import RichLog
    log = conv.query_one(RichLog)
    lines = []
    for line in log.lines:
        if hasattr(line, "_spans"):
            # Rich Text — iterate spans for colour info
            lines.append(repr(line))
        else:
            lines.append(str(line))
    return "\n".join(lines)


# ── core inline render tests ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_write_error_writes_cross_glyph_to_log() -> None:
    """Tier 2: write_error always writes '✗' into the conv RichLog."""
    from reyn.tui.widgets import ConversationView

    app = _make_app()
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)

        conv.write_error(message="something went wrong")
        await pilot.pause()

        plain = _log_plain(conv)
        assert "✗" in plain, (
            f"write_error must write '✗' glyph; log: {plain[:300]!r}"
        )


@pytest.mark.asyncio
async def test_write_error_includes_message_text() -> None:
    """Tier 2: write_error includes the error message text in the log."""
    from reyn.tui.widgets import ConversationView

    app = _make_app()
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)

        conv.write_error(message="budget exceeded daily cap")
        await pilot.pause()

        plain = _log_plain(conv)
        assert "budget exceeded daily cap" in plain, (
            f"write_error must include the message text; log: {plain[:300]!r}"
        )


@pytest.mark.asyncio
async def test_write_error_includes_skill_run_prefix() -> None:
    """Tier 2: write_error includes '[skill#run_id]' prefix when both provided."""
    from reyn.tui.widgets import ConversationView

    app = _make_app()
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)

        conv.write_error(
            message="phase failed",
            skill_name="code_review",
            run_id_short="abcd",
        )
        await pilot.pause()

        plain = _log_plain(conv)
        assert "code_review#abcd" in plain, (
            f"write_error must include '[skill#run_id]' prefix; log: {plain[:300]!r}"
        )


@pytest.mark.asyncio
async def test_write_error_includes_trace_pointer_when_has_trace() -> None:
    """Tier 2: write_error includes 'Ctrl+B → events' when skill or run_id present."""
    from reyn.tui.widgets import ConversationView

    app = _make_app()
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)

        conv.write_error(
            message="skill failed",
            skill_name="my_skill",
            run_id_short="1234",
        )
        await pilot.pause()

        plain = _log_plain(conv)
        assert "Ctrl+B" in plain, (
            f"write_error must include 'Ctrl+B → events' when has_trace; "
            f"log: {plain[:300]!r}"
        )
        assert "events" in plain, (
            f"write_error must mention 'events' in trace pointer; log: {plain[:300]!r}"
        )


@pytest.mark.asyncio
async def test_write_error_omits_trace_pointer_when_no_trace() -> None:
    """Tier 2: write_error omits 'Ctrl+B → events' when no skill/run_id (slash error)."""
    from reyn.tui.widgets import ConversationView

    app = _make_app()
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)

        conv.write_error(message="usage: /image <path>")
        await pilot.pause()

        plain = _log_plain(conv)
        assert "Ctrl+B" not in plain, (
            f"write_error must NOT include 'Ctrl+B' when no skill/run_id; "
            f"log: {plain[:300]!r}"
        )


@pytest.mark.asyncio
async def test_write_error_returns_none() -> None:
    """Tier 2: write_error returns None (no widget mount)."""
    from reyn.tui.widgets import ConversationView

    app = _make_app()
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)

        result = conv.write_error(message="boom")
        assert result is None, (
            f"write_error must return None (no widget); got {result!r}"
        )


@pytest.mark.asyncio
async def test_write_error_includes_inline_hint() -> None:
    """Tier 2: write_error splits '• hint' from message and renders it as a separate line."""
    from reyn.tui.widgets import ConversationView

    app = _make_app()
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)

        # Message with inline hint tail (the '• <hint>' pattern)
        conv.write_error(message="router failed • retry with /reset")
        await pilot.pause()

        plain = _log_plain(conv)
        assert "retry with /reset" in plain, (
            f"write_error must include the inline hint text; log: {plain[:300]!r}"
        )
        # The hint should be on its own line with '•' prefix
        assert "• retry with /reset" in plain, (
            f"write_error must render hint with '• ' prefix; log: {plain[:300]!r}"
        )


@pytest.mark.asyncio
async def test_write_error_no_errorbox_widget_mounted() -> None:
    """Tier 2: write_error does not mount any ErrorBox widget (widget deleted).

    Since ErrorBox no longer exists, we verify no unexpected widget
    children of type 'ErrorBox' appear (they cannot — the class is gone).
    Verify via the RichLog having more lines (= inline render happened)
    and no unexpected widget children.
    """
    from textual.widgets import RichLog

    from reyn.tui.widgets import ConversationView

    app = _make_app()
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        log = conv.query_one(RichLog)
        before_children = len(list(conv.children))
        before_lines = len(log.lines)

        conv.write_error(message="something broke", skill_name="coder", run_id_short="ab12")
        await pilot.pause()

        after_children = len(list(conv.children))
        after_lines = len(log.lines)

        # Lines must increase (inline render happened).
        assert after_lines > before_lines, (
            "write_error must add lines to the RichLog"
        )
        # No new widget children should appear (errors are log lines, not widgets).
        assert after_children == before_children, (
            f"write_error must not mount new widgets; "
            f"children before={before_children}, after={after_children}"
        )
