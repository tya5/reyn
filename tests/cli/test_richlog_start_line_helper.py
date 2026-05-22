"""Tier 2: _richlog_start_line wraps the private Textual attribute (G-F15).

Wave-10 follow-up Topic G finding F15 (P2): the turn-navigation
anchor system depended on ``getattr(log, "_start_line", 0)`` at
four callsites. ``_start_line`` is a private RichLog attribute
that Textual could rename / restructure on any upgrade. The bare
``getattr`` form would silently return 0 — making all anchor
positions degrade to "treated as never-trimmed", with every
Ctrl+P/N landing on the wrong line in long sessions and the
trim-warning never firing.

The fix wraps the access in ``_richlog_start_line(log)`` which
logs a one-shot warning when the attribute is missing. The
behavioural fallback stays 0 (= same as the bare getattr default)
so a no-history session keeps working, but operators get a
detectable signal that the Textual integration is broken.

Public surfaces tested:
  - normal call (attribute present) returns the value (regression
    guard)
  - missing attribute returns 0 (fallback preserved)
  - missing attribute logs a one-shot warning
  - subsequent missing-attribute calls do NOT re-log
    (dedup via ``_start_line_warned``)
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


class _StubLog:
    """RichLog stand-in carrying or missing the ``_start_line`` attribute."""

    def __init__(self, *, start_line=None) -> None:
        if start_line is not None:
            self._start_line = start_line
        self.lines: list = []


@pytest.mark.asyncio
async def test_normal_call_returns_attribute_value() -> None:
    """Tier 2 (regression): present ``_start_line`` value is returned."""
    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets import ConversationView

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        log = _StubLog(start_line=137)
        assert conv._richlog_start_line(log) == 137


@pytest.mark.asyncio
async def test_missing_attribute_returns_zero_fallback() -> None:
    """Tier 2: missing ``_start_line`` → 0 fallback (same as legacy getattr)."""
    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets import ConversationView

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        # Reset warning state in case the previous test fired it.
        conv._start_line_warned = False
        log = _StubLog()  # no _start_line attribute
        assert conv._richlog_start_line(log) == 0


@pytest.mark.asyncio
async def test_missing_attribute_logs_one_shot_warning(caplog) -> None:
    """Tier 2: first missing call logs warning, subsequent calls don't.

    The dedup gives operators a detectable signal without spamming
    the log on every turn navigation when Textual changes its
    private API.
    """
    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets import ConversationView

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        conv._start_line_warned = False
        log = _StubLog()

        with caplog.at_level(logging.WARNING):
            caplog.clear()
            conv._richlog_start_line(log)
            first_count = sum(
                1 for r in caplog.records
                if "RichLog._start_line is missing" in r.getMessage()
            )
            # Three more calls — should NOT log again.
            conv._richlog_start_line(log)
            conv._richlog_start_line(log)
            conv._richlog_start_line(log)
            total_count = sum(
                1 for r in caplog.records
                if "RichLog._start_line is missing" in r.getMessage()
            )

        assert first_count == 1, (
            f"first missing-attr call should log exactly once; "
            f"got {first_count}"
        )
        assert total_count == 1, (
            f"subsequent missing-attr calls should not re-log; "
            f"total log lines: {total_count}"
        )


@pytest.mark.asyncio
async def test_absolute_line_position_uses_helper() -> None:
    """Tier 2: ``_absolute_line_position`` reads through ``_richlog_start_line``.

    Regression guard for the refactor — the static helper became an
    instance method using ``self._richlog_start_line(log)`` so the
    one-shot warning path covers ``_absolute_line_position`` too.
    """
    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets import ConversationView

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        log = _StubLog(start_line=42)
        # absolute = start_line + len(log.lines); lines is empty, so 42.
        assert conv._absolute_line_position(log) == 42
        log.lines = [1, 2, 3]  # 3 entries
        assert conv._absolute_line_position(log) == 45
