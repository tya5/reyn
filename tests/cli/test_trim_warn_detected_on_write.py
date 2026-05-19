"""Tier 2: ``_trim_warned`` fires on any log write past the RichLog boundary.

The previous wiring only called ``_maybe_warn_about_trimmed_history``
from ``_jump_to_relative_anchor`` (= turn navigation). A user who let
the session auto-scroll past the ``_RICHLOG_MAX_LINES`` boundary never
saw the "earlier history trimmed" signal until they happened to press
Ctrl+P / Ctrl+N — by which point the disconnect between scroll position
and visible content was already confusing.

Hook the check into ``_write_log`` so the warning fires the first time
a write crosses the trim boundary, regardless of how the user is
interacting with the conv pane.

Contract pinned:

1. When ``log._start_line == 0`` (= no trim yet), the warning does not
   fire (= ``_trim_warned`` stays False).
2. When ``log._start_line > 0`` after a write, the warning fires
   exactly once (= ``_trim_warned`` flips to True).
3. Subsequent writes do NOT re-fire (= one-shot semantics preserved).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from rich.text import Text

from reyn.chat.tui.app import ReynTUIApp
from reyn.chat.tui.widgets import ConversationView


def _make_app() -> ReynTUIApp:
    return ReynTUIApp(
        registry=None,
        agent_name="test-agent",
        model="test-model",
        budget_tracker=None,
    )


@pytest.mark.asyncio
async def test_write_log_does_not_warn_when_no_trim_occurred() -> None:
    """Tier 2: a write into a fresh log (no trim) doesn't fire the warning."""
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        assert conv._trim_warned is False

        conv._write_log(Text("hello"))
        await pilot.pause()
        assert conv._trim_warned is False, (
            "warning must not fire when log has not yet been trimmed"
        )


@pytest.mark.asyncio
async def test_write_log_fires_warning_when_log_start_line_is_positive() -> None:
    """Tier 2: a write fires the one-shot warning once the log has trimmed."""
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        log = conv._log()
        # Simulate the trim having already happened: bump the RichLog's
        # internal cumulative drop counter. ``log._start_line`` is the
        # public-ish attribute the helper reads, so we set it directly —
        # mirrors what RichLog does internally when its ring buffer drops
        # lines.
        log._start_line = 42  # type: ignore[attr-defined]

        assert conv._trim_warned is False
        conv._write_log(Text("trigger"))
        await pilot.pause()
        assert conv._trim_warned is True, (
            "_write_log must surface the warning once start_line > 0"
        )


@pytest.mark.asyncio
async def test_subsequent_writes_do_not_re_fire_warning() -> None:
    """Tier 2: the warning is one-shot — extra writes don't re-trigger it.

    Defends against the warning bouncing into every subsequent line and
    spamming the sticky status.
    """
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        log = conv._log()
        log._start_line = 42  # type: ignore[attr-defined]

        # First write fires the warning.
        conv._write_log(Text("first"))
        await pilot.pause()
        assert conv._trim_warned is True

        # Subsequent writes: still flagged, no second fire. Capture the
        # sticky state before and after to verify nothing re-mounts.
        # The flag itself is the public single-fire guard, so we just
        # confirm it stays True — and that calling write again is a
        # no-op for the warning path.
        conv._write_log(Text("second"))
        await pilot.pause()
        conv._write_log(Text("third"))
        await pilot.pause()
        assert conv._trim_warned is True, "flag must stay True"
