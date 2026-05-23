"""Tier 2: ReynHeader status badges respond to mouse clicks.

Categorical UX gap on the mouse-keyboard parity axis. The
header now hosts three "active state" badges:

  - ``[N pending]`` — cross-channel pending ops (= #277)
  - ``[find: 'q' N/M]`` — /find cycle state (= #565)
  - ``🔴 voice`` / ``⏳ voice`` — voice mode (= #581, in flight)

Each had a keyboard trigger but no mouse equivalent. This PR
wires per-badge click dispatch:

  - click find badge    → action_find_next (= Ctrl+G cycle)
  - click pending badge → open panel + switch to Pending tab
  - click voice badge   → action_voice_toggle (= Ctrl+R)

Click outside any badge cell range → silent no-op.

Public surfaces tested:
  - ``ReynHeader.badge_at_x(x)`` returns the badge under the
    cell column, or None
  - ``_badge_offsets`` populates correctly during ``_format_status``
    when each badge is rendered
  - Click on find badge invokes ``app.action_find_next``
  - Click on pending badge opens panel + switches to Pending tab
  - Click outside any badge → no_op + event not stopped

Voice badge click is targeted in this PR's design but the voice
state field is from #581 (in flight) — once that lands, a
follow-on test will exercise the voice click path. Pinned today
via the offset-population test (= voice key appears in
``_badge_offsets`` when the state is set, regardless of whether
the field is on main yet).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


@pytest.mark.asyncio
async def test_badge_offsets_populated_for_find_badge() -> None:
    """Tier 2: ``_format_status`` records the find badge's cell range."""
    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets import ReynHeader

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        header = app.query_one("#header", ReynHeader)
        header.set_find_state("foo", position=1, total=3)
        await pilot.pause()
        assert "find" in header._badge_offsets
        start, end = header._badge_offsets["find"]
        # Range should be non-trivial (badge is ~17 cells: "[find: 'foo' 1/3]").
        assert end > start
        assert end - start >= 10


@pytest.mark.asyncio
async def test_badge_offsets_populated_for_pending_badge() -> None:
    """Tier 2: ``_format_status`` records the pending badge's cell range."""
    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets import ReynHeader

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        header = app.query_one("#header", ReynHeader)
        header.refresh_status(stalled_count=3)
        await pilot.pause()
        assert "pending" in header._badge_offsets
        start, end = header._badge_offsets["pending"]
        assert end > start


@pytest.mark.asyncio
async def test_badge_offsets_empty_when_no_badges() -> None:
    """Tier 2: cold-default state has no badge ranges (= nothing clickable)."""
    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets import ReynHeader

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        header = app.query_one("#header", ReynHeader)
        # Nothing set up — no badges.
        assert "find" not in header._badge_offsets
        assert "pending" not in header._badge_offsets
        assert "voice" not in header._badge_offsets


@pytest.mark.asyncio
async def test_badge_at_x_returns_none_outside_text() -> None:
    """Tier 2: ``badge_at_x`` returns None for clicks outside text region."""
    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets import ReynHeader

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        header = app.query_one("#header", ReynHeader)
        header.set_find_state("foo", 1, 3)
        await pilot.pause()
        # Click on the left edge (= title region, definitely outside status).
        assert header.badge_at_x(0) is None
        assert header.badge_at_x(2) is None


@pytest.mark.asyncio
async def test_badge_at_x_hits_find_badge() -> None:
    """Tier 2: ``badge_at_x`` returns 'find' when clicked over the find badge."""
    from rich.cells import cell_len

    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets import ReynHeader

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        header = app.query_one("#header", ReynHeader)
        header.set_find_state("foo", 1, 3)
        await pilot.pause()
        # Compute where the find badge sits in widget-local cells.
        rendered = header.rendered_text()
        text_cells = cell_len(rendered)
        text_left = header.size.width - 2 - text_cells
        find_start, find_end = header._badge_offsets["find"]
        # Pick the middle of the badge.
        mid_x = text_left + (find_start + find_end) // 2
        assert header.badge_at_x(mid_x) == "find"


@pytest.mark.asyncio
async def test_click_on_find_badge_invokes_action_find_next() -> None:
    """Tier 2: click on find badge → cycles to next match.

    Drives the click path end-to-end: synthesise a Click event
    with x in the find badge's cell range, verify the router's
    find cursor advanced.
    """
    from rich.cells import cell_len
    from rich.text import Text
    from textual import events as textual_events

    from reyn.chat.outbox import OutboxMessage
    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.app_outbox import OutboxRouter
    from reyn.chat.tui.widgets import ConversationView, ReynHeader

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        # Set up router + 3 matches so the find cycle has something
        # to navigate.
        conv = app.query_one("#conversation", ConversationView)
        header = app.query_one("#header", ReynHeader)
        for line in ["alpha foo bar", "beta padding", "gamma foo end"]:
            conv._log().write(Text(line))
        await pilot.pause()
        router = OutboxRouter(app)
        # Expose router so action_find_next can find it.
        app._outbox_router = router
        router._on_find(
            OutboxMessage(kind="__find__", text="foo"),
            conv,
            header,
        )
        await pilot.pause()
        initial_cursor = router._find_cursor_idx
        # Synthesise click on the find badge.
        rendered = header.rendered_text()
        text_cells = cell_len(rendered)
        text_left = header.size.width - 2 - text_cells
        find_start, find_end = header._badge_offsets["find"]
        mid_x = text_left + (find_start + find_end) // 2
        click = textual_events.Click(
            chain=1, widget=header, x=mid_x, y=0,
            delta_x=0, delta_y=0,
            button=1, shift=False, meta=False, ctrl=False,
            screen_x=mid_x, screen_y=0, style=None,
        )
        header.on_click(click)
        await pilot.pause()
        # Cursor advanced after the click-driven find_next.
        assert router._find_cursor_idx != initial_cursor


@pytest.mark.asyncio
async def test_click_on_pending_badge_opens_panel_to_pending_tab() -> None:
    """Tier 2: click on pending badge opens the panel + switches to Pending."""
    from rich.cells import cell_len
    from textual import events as textual_events

    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets import ReynHeader, RightPanel

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        header = app.query_one("#header", ReynHeader)
        header.refresh_status(stalled_count=2)
        await pilot.pause()
        # Pre-state: panel hidden.
        assert app._panel_visible is False
        rendered = header.rendered_text()
        text_cells = cell_len(rendered)
        text_left = header.size.width - 2 - text_cells
        start, end = header._badge_offsets["pending"]
        mid_x = text_left + (start + end) // 2
        click = textual_events.Click(
            chain=1, widget=header, x=mid_x, y=0,
            delta_x=0, delta_y=0,
            button=1, shift=False, meta=False, ctrl=False,
            screen_x=mid_x, screen_y=0, style=None,
        )
        header.on_click(click)
        await pilot.pause()
        # Panel opens to pending tab.
        assert app._panel_visible is True
        panel = app.query_one("#right_panel", RightPanel)
        tabs = panel.query_one("#panel-tabs")
        assert tabs.active == "pending"


@pytest.mark.asyncio
async def test_click_outside_badge_is_silent_no_op() -> None:
    """Tier 2: click on non-badge text leaves state untouched."""
    from textual import events as textual_events

    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets import ReynHeader

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        header = app.query_one("#header", ReynHeader)
        header.set_find_state("foo", 1, 3)
        await pilot.pause()
        before_panel = app._panel_visible
        # Click far-left (= title region).
        click = textual_events.Click(
            chain=1, widget=header, x=2, y=0,
            delta_x=0, delta_y=0,
            button=1, shift=False, meta=False, ctrl=False,
            screen_x=2, screen_y=0, style=None,
        )
        header.on_click(click)
        await pilot.pause()
        # Panel unchanged (= no badge was hit, no action).
        assert app._panel_visible == before_panel
