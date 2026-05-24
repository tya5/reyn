"""Tier 2: terminal-error sticky persists after ErrorBox mount (C[2] race fix).

Wave-13 cascade audit finding C[2]: in app_outbox._on_error, the original
code for severity=="high" called conv.show_status("✗ terminal error",
terminal=True) BEFORE conv.render_message(msg).  But render_message calls
mount_error, which internally calls self.hide_status() (unconditional when
not scrolled).  Result: the sticky was shown then immediately hidden in the
same call stack — effective display time ZERO.

Fixed by reordering:
  1. conv.render_message(msg) first (= mount ErrorBox + mount_error's
     hide_status fires here).
  2. conv.show_status("✗ terminal error", terminal=True) after (= now
     persists because mount_error's hide_status has already run).

Pinned invariants:
  1. _on_error with a "high"-severity message → after dispatch the sticky
     is active, body contains "terminal error", priority == 110.
  2. An ErrorBox is also mounted (= both side effects achieved, not just
     the sticky).
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
    from reyn.chat.tui.app import ReynTUIApp
    return ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)


def _make_high_severity_msg():
    """Build an error OutboxMessage that _classify_error_severity rates 'high'.

    Uses the ``[budget exceeded]`` text marker (one of the _HIGH_TEXT_MARKERS).
    meta["skill"] is present so the render_message path mounts an ErrorBox
    (= the error-kind branch in render_message).
    """
    from reyn.chat.outbox import OutboxMessage
    return OutboxMessage(
        kind="error",
        text="[budget exceeded] daily token cap reached",
        meta={"skill": "coder"},
    )


def _make_med_severity_msg():
    """Build a plain error OutboxMessage that rates 'med' severity."""
    from reyn.chat.outbox import OutboxMessage
    return OutboxMessage(
        kind="error",
        text="something went wrong (transient)",
        meta={"skill": "coder"},
    )


# ── test 1: high-severity → sticky active, priority==110, "terminal error" ────


@pytest.mark.asyncio
async def test_high_severity_error_sticky_persists_after_mount() -> None:
    """Tier 2: high-severity _on_error → sticky active with priority 110, body 'terminal error'.

    Verifies the C[2] race fix: the sticky must survive the hide_status()
    call inside mount_error because render_message is now called first.
    """
    from reyn.chat.tui.app_outbox import OutboxRouter
    from reyn.chat.tui.widgets import ConversationView, ReynHeader

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


# ── test 2: ErrorBox also mounted (both side effects) ─────────────────────────


@pytest.mark.asyncio
async def test_high_severity_error_also_mounts_error_box() -> None:
    """Tier 2: high-severity _on_error mounts an ErrorBox alongside the sticky.

    Both effects must occur: the sticky (test 1) and the ErrorBox in the
    conv pane.  Verifies via ConversationView._error_boxes list length.
    """
    from reyn.chat.tui.app_outbox import OutboxRouter
    from reyn.chat.tui.widgets import ConversationView, ReynHeader

    app = _make_app()

    async with app.run_test(headless=True) as pilot:
        await pilot.pause()

        app._get_session = lambda: None  # type: ignore[method-assign]

        conv = app.query_one("#conversation", ConversationView)
        header = app.query_one("#header", ReynHeader)
        router = OutboxRouter(app)

        # Pre-condition: no ErrorBox children under ConversationView yet.
        from reyn.chat.tui.widgets.error_box import ErrorBox
        pre_boxes = conv.query(ErrorBox)
        assert not pre_boxes, "pre-condition: no ErrorBoxes mounted yet"

        router._on_error(_make_high_severity_msg(), conv, header)
        await pilot.pause()

        # At least one ErrorBox must now exist as a child of conv.
        post_boxes = conv.query(ErrorBox)
        assert post_boxes, (
            "at least one ErrorBox must be mounted after high-severity _on_error"
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
    from reyn.chat.tui.app_outbox import OutboxRouter
    from reyn.chat.tui.widgets import ConversationView, ReynHeader

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
