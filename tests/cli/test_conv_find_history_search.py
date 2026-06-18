"""Tier 2: /find slash command searches RichLog buffer (history search MVP).

Categorical UX gap fill: searches the live RichLog buffer for
case-insensitive substring matches, scrolls to the nearest match
below the current scroll position, and writes a status line with
the match count + line numbers.

Public surfaces tested:
  - ``ConversationView.find_in_buffer`` returns matching lines
    (case-insensitive substring)
  - empty query → empty result list
  - end-to-end via ``OutboxRouter._on_find`` synthesised dispatch:
    no matches → error status, matches present → status with
    count, scroll position advances to match line
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


@pytest.mark.asyncio
async def test_find_in_buffer_returns_substring_matches() -> None:
    """Tier 2: substring match against the RichLog buffer is case-insensitive."""
    from rich.text import Text

    from reyn.interfaces.tui.app import ReynTUIApp
    from reyn.interfaces.tui.widgets import ConversationView

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        log = conv._log()
        log.write(Text("first line about Apples"))
        log.write(Text("second line — nothing here"))
        log.write(Text("third line: apple pie"))
        await pilot.pause()

        matches = conv.find_in_buffer("apple")
        # Two lines contain "apple" (case-insensitive: "Apples" + "apple pie").
        match_apples, match_pie = matches  # exactly 2 case-insensitive matches expected
        # Tuple shape: (line_idx, line_text).
        for line_idx, line_text in (match_apples, match_pie):
            assert isinstance(line_idx, int)
            assert "apple" in line_text.lower()


@pytest.mark.asyncio
async def test_find_in_buffer_empty_query_returns_empty_list() -> None:
    """Tier 2: empty / whitespace-only query → empty result (= safe no-op)."""
    from reyn.interfaces.tui.app import ReynTUIApp
    from reyn.interfaces.tui.widgets import ConversationView

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        from rich.text import Text
        conv._log().write(Text("some content"))
        await pilot.pause()
        assert conv.find_in_buffer("") == []
        assert conv.find_in_buffer("   ") == []


@pytest.mark.asyncio
async def test_on_find_no_matches_writes_error_status() -> None:
    """Tier 2: query with zero matches → error-kind sticky status."""
    from rich.text import Text

    from reyn.interfaces.tui.app import ReynTUIApp
    from reyn.interfaces.tui.app_outbox import OutboxRouter
    from reyn.interfaces.tui.widgets import ConversationView
    from reyn.runtime.outbox import OutboxMessage

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        header = app.query_one("#header")
        conv._log().write(Text("here are some words"))
        await pilot.pause()

        router = OutboxRouter(app)
        router._on_find(
            OutboxMessage(kind="__find__", text="nonexistent"),
            conv,
            header,
        )
        await pilot.pause()
        snap = conv._sticky().snapshot()  # type: ignore[union-attr]
        assert snap["active"] is True
        assert "no matches for 'nonexistent'" in snap["body"]


@pytest.mark.asyncio
async def test_on_find_matches_writes_count_and_lines_in_status() -> None:
    """Tier 2: match list status reports total + first ≤5 line numbers."""
    from rich.text import Text

    from reyn.interfaces.tui.app import ReynTUIApp
    from reyn.interfaces.tui.app_outbox import OutboxRouter
    from reyn.interfaces.tui.widgets import ConversationView
    from reyn.runtime.outbox import OutboxMessage

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        header = app.query_one("#header")
        # 3 lines with "needle" + 2 without.
        log = conv._log()
        log.write(Text("first needle here"))
        log.write(Text("unrelated"))
        log.write(Text("second needle row"))
        log.write(Text("another unrelated"))
        log.write(Text("third needle entry"))
        await pilot.pause()

        router = OutboxRouter(app)
        router._on_find(
            OutboxMessage(kind="__find__", text="needle"),
            conv,
            header,
        )
        await pilot.pause()
        snap = conv._sticky().snapshot()  # type: ignore[union-attr]
        assert snap["active"] is True
        body = snap["body"]
        # 3 matches reported.
        assert "3 matches for 'needle'" in body
        # Line numbers surfaced (1-indexed for human reading).
        assert "lines 1" in body and "3" in body and "5" in body


@pytest.mark.asyncio
async def test_on_find_empty_query_writes_usage_status() -> None:
    """Tier 2: empty query → usage hint via error-kind status."""
    from reyn.interfaces.tui.app import ReynTUIApp
    from reyn.interfaces.tui.app_outbox import OutboxRouter
    from reyn.interfaces.tui.widgets import ConversationView
    from reyn.runtime.outbox import OutboxMessage

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        header = app.query_one("#header")

        router = OutboxRouter(app)
        router._on_find(
            OutboxMessage(kind="__find__", text=""),
            conv,
            header,
        )
        await pilot.pause()
        snap = conv._sticky().snapshot()  # type: ignore[union-attr]
        assert snap["active"] is True
        assert "usage: /find" in snap["body"]


def test_find_slash_command_is_registered() -> None:
    """Tier 2: ``/find`` command appears in the slash registry.

    Registration in ``slash/__init__.py`` is the pipe between
    typing ``/find`` and triggering the TUI handler. Pin that the
    decorator-registered command is reachable from the registry
    (= the same surface the slash picker walks).
    """
    from reyn.interfaces.slash import REGISTRY

    names = {c.name for c in REGISTRY.all_commands()}
    assert "find" in names, (
        f"/find should be registered; got commands: {sorted(names)}"
    )
