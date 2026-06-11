"""Tier 2: StickyStatus overwrite-priority + trim-warning log line (G-F8 + I-F8).

Wave-10 G-F8 + I-F8 (P1, bundled — single root cause: ``show()`` had
no priority guard, so transient breadcrumbs silently overwrote
load-bearing live indicators).

  - **I-F8**: Ctrl+P / Ctrl+N during an LLM call replaced the
    ``⟳ thinking…`` indicator with ``↑ turn 3 / 8``, making the agent
    appear frozen until the next outbox event arrived.
  - **G-F8**: ``_maybe_warn_about_trimmed_history`` wrote to the sticky,
    then ``_flash_turn_position`` fired in the same call frame and
    overwrote the warning before the user could read it.

Fix:
  - ``StickyStatus.show()`` respects a ``_KIND_PRIORITY`` map.
    ``error`` (priority 80) cannot be displaced by ``general``
    (priority 50); same-or-higher priority overwrites freely.
  - ``_maybe_warn_about_trimmed_history`` additionally writes a
    permanent dim log line so the warning survives subsequent
    sticky overwrites — the user can find it in scrollback.

Note: the original tests checked ``kind="thinking"`` (priority 100).
The ``"thinking"`` kind was removed when the inline Braille spinner
(``InlineThinkingRow``) replaced the sticky thinking indicator. The
priority hierarchy now has ``error`` (80) as the highest kind.

Public surfaces tested:
  - error-active + general show → suppressed (priority guard)
  - error → error overwrite → succeeds (regression guard)
  - general-active + error show → succeeds (priority elevation)
  - general → general overwrite → succeeds (same priority)
  - trim warning emits both a sticky AND a permanent log line (G-F8)
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


async def _sticky(pilot):
    """Get the StickyStatus mounted under the ConversationView."""
    from reyn.chat.tui.widgets import ConversationView
    conv = pilot.app.query_one("#conversation", ConversationView)
    return conv._sticky()


@pytest.mark.asyncio
async def test_general_show_suppressed_when_error_active() -> None:
    """Tier 2b: a general flash cannot overwrite an active error (I-F8)."""
    from reyn.chat.tui.app import ReynTUIApp

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        s = await _sticky(pilot)
        s.show("copy failed", kind="error")
        assert s.snapshot()["kind"] == "error"
        assert s.snapshot()["body"] == "copy failed"

        # Lower-priority "general" must NOT displace error.
        s.show("↑ turn 3 / 8", kind="general")
        snap = s.snapshot()
        assert snap["kind"] == "error", (
            f"error should still be active, got kind={snap['kind']!r}"
        )
        assert snap["body"] == "copy failed", (
            f"error body should be preserved, got body={snap['body']!r}"
        )


@pytest.mark.asyncio
async def test_error_overwrites_error_body() -> None:
    """Tier 2: same-priority overwrite still works (regression guard).

    The natural error-body update must not be suppressed.
    """
    from reyn.chat.tui.app import ReynTUIApp

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        s = await _sticky(pilot)
        s.show("first error", kind="error")
        s.show("second error", kind="error")
        snap = s.snapshot()
        assert snap["kind"] == "error"
        assert snap["body"] == "second error"


@pytest.mark.asyncio
async def test_error_overwrites_general_when_general_active() -> None:
    """Tier 2: higher-priority show DISPLACES lower-priority active."""
    from reyn.chat.tui.app import ReynTUIApp

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        s = await _sticky(pilot)
        s.show("↑ turn 2 / 5", kind="general")
        assert s.snapshot()["kind"] == "general"
        s.show("copy failed", kind="error")
        snap = s.snapshot()
        assert snap["kind"] == "error"
        assert snap["body"] == "copy failed"


@pytest.mark.asyncio
async def test_general_overwrites_general() -> None:
    """Tier 2: same-priority general → general overwrites (turn-flash chain)."""
    from reyn.chat.tui.app import ReynTUIApp

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        s = await _sticky(pilot)
        s.show("↑ turn 1 / 5", kind="general")
        s.show("↑ turn 2 / 5", kind="general")
        snap = s.snapshot()
        assert snap["kind"] == "general"
        assert snap["body"] == "↑ turn 2 / 5"


@pytest.mark.asyncio
async def test_trim_warning_writes_permanent_log_line() -> None:
    """Tier 2b: trim warning survives subsequent sticky overwrite (G-F8).

    Pre-fix the warning was sticky-only and ``_flash_turn_position`` in
    the same call frame replaced it. Post-fix the warning is also
    written as a permanent dim log line that survives any sticky
    update.
    """
    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets import ConversationView

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        log = conv._log()

        # Simulate ring-buffer trim: ``_start_line`` would be > 0 after
        # the RichLog has dropped earlier history. Patch it directly
        # for the test — the public ``_maybe_warn_about_trimmed_history``
        # reads via ``getattr(log, "_start_line", 0)``.
        log._start_line = 137  # type: ignore[attr-defined]
        assert not conv.trim_warned

        conv._scroll_ctrl._maybe_warn_about_trimmed_history(log)
        await pilot.pause()

        # One-shot — the flag flips so subsequent calls are no-ops.
        assert conv.trim_warned

        # The permanent log line was written.
        log_lines = [
            getattr(strip, "text", "")
            for strip in getattr(log, "lines", [])
        ]
        joined = "\n".join(log_lines)
        assert "earlier history trimmed" in joined, (
            f"trim warning should appear as a permanent log line; "
            f"got:\n{joined!r}"
        )
        assert "137" in joined, "trim count should be formatted in the warning"

        # Sticky glance-cue is also active right now.
        snap = conv._sticky().snapshot()
        assert snap["active"] is True
        assert "earlier history trimmed" in snap["body"]

        # Now simulate the same-frame overwrite that previously hid the
        # warning: a general-kind flash with a different body.
        conv.show_status("↑ turn 1 / 5", kind="general")
        snap_after = conv._sticky().snapshot()
        assert "turn 1 / 5" in snap_after["body"]  # sticky was overwritten
        # But the log line is still there.
        log_lines_after = [
            getattr(strip, "text", "")
            for strip in getattr(log, "lines", [])
        ]
        assert any(
            "earlier history trimmed" in line for line in log_lines_after
        ), "trim warning log line must persist after sticky overwrite"
