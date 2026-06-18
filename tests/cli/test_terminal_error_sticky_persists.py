"""Tier 2: terminal-error sticky persists after inline write_error (C[2] race fix).

Wave-13 cascade audit finding C[2]: in app_outbox._on_error, render_message
(= write_error) must be called BEFORE show_status("✗ terminal error",
terminal=True), because write_error's own hide_status() would suppress
the terminal sticky if called after.

Pinned invariants:
  1. _on_error with a "high"-severity message → after dispatch the sticky
     is active, body contains "terminal error", priority == 110.
  2. The conv log contains the error text (inline render happened).
  3. A "med"-severity message (normal error path) → sticky is NOT
     showing the terminal-error message (scope guard: only high-severity
     triggers the terminal sticky).

No MagicMock / AsyncMock / patch.  Real ReynTUIApp + ConversationView.
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
    from reyn.interfaces.tui.app import ReynTUIApp
    return ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)


def _make_high_severity_msg():
    """Build an error OutboxMessage that _classify_error_severity rates 'high'.

    Uses the ``[budget exceeded]`` text marker (one of the _HIGH_TEXT_MARKERS).
    """
    from reyn.runtime.outbox import OutboxMessage
    return OutboxMessage(
        kind="error",
        text="[budget exceeded] daily token cap reached",
        meta={"skill": "coder"},
    )


def _make_med_severity_msg():
    """Build a plain error OutboxMessage that rates 'med' severity."""
    from reyn.runtime.outbox import OutboxMessage
    return OutboxMessage(
        kind="error",
        text="something went wrong (transient)",
        meta={"skill": "coder"},
    )


# ── test 1: high-severity → sticky active, priority==110, "terminal error" ────


@pytest.mark.asyncio
async def test_high_severity_error_sticky_persists_after_render() -> None:
    """Tier 2: high-severity _on_error → sticky active with priority 110, body 'terminal error'.

    Verifies the C[2] race fix: the sticky must survive the hide_status()
    call inside write_error because render_message is now called first.
    """
    from reyn.interfaces.tui.app_outbox import OutboxRouter
    from reyn.interfaces.tui.widgets import ConversationView, ReynHeader

    app = _make_app()

    async with app.run_test(headless=True) as pilot:
        await pilot.pause()

        # Wire a None session so _get_session doesn't crash.
        app._get_session = lambda: None  # type: ignore[method-assign]

        conv = app.query_one("#conversation", ConversationView)
        header = app.query_one("#header", ReynHeader)
        router = OutboxRouter(app)

        router._on_error(_make_high_severity_msg(), conv, header)
        await pilot.pause()

        sticky = conv._sticky()
        assert sticky is not None, "StickyStatus must be mounted under ConversationView"

        snap = sticky.snapshot()
        assert snap["active"] is True, (
            f"sticky must be active after high-severity error, got active={snap['active']!r}"
        )
        assert "terminal error" in snap["body"], (
            f"sticky body must contain 'terminal error', got {snap['body']!r}"
        )
        assert snap["priority"] == 110, (
            f"terminal sticky must report priority==110, got {snap['priority']!r}"
        )


# ── test 2: error text appears inline in the conv log ─────────────────────────


@pytest.mark.asyncio
async def test_high_severity_error_renders_inline_in_log() -> None:
    """Tier 2: high-severity _on_error writes the error text inline in the conv log.

    Errors are now plain RichLog lines (no widget mount). Verify the
    conv log contains the '✗' glyph from write_error's header line.
    Public surface: iterate log.lines (RichLog's public buffer) and check
    that at least one line's plain text contains '✗'.
    """
    from reyn.interfaces.tui.app_outbox import OutboxRouter
    from reyn.interfaces.tui.widgets import ConversationView, ReynHeader

    app = _make_app()

    async with app.run_test(headless=True) as pilot:
        await pilot.pause()

        app._get_session = lambda: None  # type: ignore[method-assign]

        conv = app.query_one("#conversation", ConversationView)
        header = app.query_one("#header", ReynHeader)
        router = OutboxRouter(app)

        router._on_error(_make_high_severity_msg(), conv, header)
        await pilot.pause()

        log = conv._log()
        # RichLog.lines is a public list of Text renderables.
        log_plain = "\n".join(
            line.plain if hasattr(line, "plain") else str(line)
            for line in log.lines
        )
        assert "✗" in log_plain, (
            f"conv log must contain '✗' error glyph from write_error; "
            f"log_plain snippet: {log_plain[:300]!r}"
        )
        assert "budget exceeded" in log_plain, (
            f"conv log must contain the error text; "
            f"log_plain snippet: {log_plain[:300]!r}"
        )


# ── test 3: med-severity does NOT show terminal sticky ────────────────────────


@pytest.mark.asyncio
async def test_med_severity_error_does_not_show_terminal_sticky() -> None:
    """Tier 2: med-severity _on_error does NOT produce a 'terminal error' sticky.

    Scope guard: only high-severity errors trigger the terminal sticky.
    A med-severity error (the common case) must not carry priority==110
    or the 'terminal error' body — that would escalate every transient
    error to terminal prominence.
    """
    from reyn.interfaces.tui.app_outbox import OutboxRouter
    from reyn.interfaces.tui.widgets import ConversationView, ReynHeader

    app = _make_app()

    async with app.run_test(headless=True) as pilot:
        await pilot.pause()

        app._get_session = lambda: None  # type: ignore[method-assign]

        conv = app.query_one("#conversation", ConversationView)
        header = app.query_one("#header", ReynHeader)
        router = OutboxRouter(app)

        router._on_error(_make_med_severity_msg(), conv, header)
        await pilot.pause()

        sticky = conv._sticky()
        if sticky is not None:
            snap = sticky.snapshot()
            if snap["active"]:
                assert "terminal error" not in snap["body"], (
                    f"med-severity must not show 'terminal error' sticky, "
                    f"got body={snap['body']!r}"
                )
                assert snap["priority"] != 110, (
                    f"med-severity sticky must not have priority 110, "
                    f"got priority={snap['priority']!r}"
                )
