"""Tier 2: Alt+Home jumps the conv log to the top (symmetric with Alt+End).

Issue #1144 #3. The conv pane had keyboard scroll for PageUp/Down, line
up/down (Alt+Up/Down), and jump-to-bottom (Alt+End) — but no jump-to-top.
The log has ``can_focus=False`` so Textual's default Home never reaches it.
This adds ``scroll_to_top`` + an Alt+Home binding/action mirroring the
existing Alt+End → ``scroll_to_bottom`` path.

Public surface only (``conv.scroll_to_top`` / ``app.action_conv_scroll_home``
+ the ``user_scrolled`` accessor + ``log.scroll_y``); no private-state asserts.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from rich.text import Text
from textual.widgets import RichLog

from reyn.chat.tui.app import ReynTUIApp
from reyn.chat.tui.widgets import ConversationView


def _make_app() -> ReynTUIApp:
    return ReynTUIApp(
        registry=None,
        agent_name="test-agent",
        model="test-model",
        budget_tracker=None,
    )


def _fill_log(log: RichLog, n_lines: int) -> None:
    for i in range(n_lines):
        log.write(Text(f"line {i}"))


@pytest.mark.asyncio
async def test_scroll_to_top_jumps_and_locks() -> None:
    """Tier 2: ``scroll_to_top`` moves to y=0 and sets the user-scrolled lock."""
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 10)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        log = conv.query_one(RichLog)
        _fill_log(log, 60)
        await pilot.pause()
        # Default: following the tail (bottom).
        assert log.max_scroll_y > 5, (
            f"test setup: need scrollback (max_scroll_y={log.max_scroll_y})"
        )

        conv.scroll_to_top()
        await pilot.pause()
        await pilot.pause()

        assert log.scroll_y == 0, f"expected top (y=0), got {log.scroll_y}"
        # Reading the oldest content = scrolled up: auto-scroll must stay off.
        assert conv.user_scrolled is True


@pytest.mark.asyncio
async def test_action_conv_scroll_home_dispatches_to_top() -> None:
    """Tier 2: the Alt+Home action wires through to ``scroll_to_top``."""
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 10)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        log = conv.query_one(RichLog)
        _fill_log(log, 60)
        await pilot.pause()

        app.action_conv_scroll_home()
        await pilot.pause()
        await pilot.pause()

        assert log.scroll_y == 0


@pytest.mark.asyncio
async def test_alt_home_binding_registered() -> None:
    """Tier 2: an Alt+Home binding is registered and maps to conv_scroll_home."""
    app = _make_app()
    hits = [
        b for b in app.BINDINGS
        if getattr(b, "key", None) == "alt+home"
        and getattr(b, "action", None) == "conv_scroll_home"
    ]
    assert hits, "Alt+Home → conv_scroll_home binding must be registered"
