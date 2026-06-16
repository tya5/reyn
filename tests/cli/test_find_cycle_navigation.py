"""Tier 2: Ctrl+G / Ctrl+Shift+G cycle through ``/find`` matches.

Follow-on to PR #537 (= ``/find`` MVP). The MVP scrolled to the
first below-cursor match and surfaced a summary status line, but
left subsequent matches reachable only by re-running ``/find``
(= no inter-match navigation). This wires:

  - ``Ctrl+G``       → forward to the next match (wraps to first
                       after the last)
  - ``Ctrl+Shift+G`` → backward to the previous match (wraps to
                       last before the first)

State lives on the ``OutboxRouter`` instance (= ``_find_query`` +
``_find_cursor_idx``) and is seeded by ``_on_find`` when a query
yields ≥ 1 match. Re-runs ``find_in_buffer`` on every cycle so
indices stay honest when the buffer mutates between presses.

Public surfaces tested:
  - ``OutboxRouter.cycle_find(+1)`` and ``cycle_find(-1)`` step
    through matches and update the sticky status with a
    ``"match N/M for '<q>' · line <ln>"`` body
  - wrap-around at both ends
  - usage hint when no prior ``/find`` query is set
  - no-matches branch when the buffer mutated and the prior
    query no longer hits anything (= cycle state is cleared so a
    fresh ``/find`` starts clean)
  - ``ctrl+g`` / ``ctrl+shift+g`` appear in the App bindings
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def _seed_lines(conv, lines: list[str]) -> None:
    """Helper: write Rich Text lines into the conv RichLog."""
    from rich.text import Text
    log = conv._log()
    for line in lines:
        log.write(Text(line))


@pytest.mark.asyncio
async def test_cycle_find_forward_steps_through_matches() -> None:
    """Tier 2: Ctrl+G after /find moves to the next match in order."""
    from reyn.chat.outbox import OutboxMessage
    from reyn.tui.app import ReynTUIApp
    from reyn.tui.app_outbox import OutboxRouter
    from reyn.tui.widgets import ConversationView

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        header = app.query_one("#header")
        _seed_lines(conv, [
            "alpha needle 1",   # idx 0
            "filler 1",         # idx 1
            "beta needle 2",    # idx 2
            "filler 2",         # idx 3
            "gamma needle 3",   # idx 4
        ])
        await pilot.pause()

        router = OutboxRouter(app)
        router._on_find(
            OutboxMessage(kind="__find__", text="needle"),
            conv,
            header,
        )
        await pilot.pause()
        # Initial /find lands on the first below-cursor match = idx 0
        assert router.find_cursor_index == 0

        # Cycle forward → next match (idx 2)
        router.cycle_find(+1)
        await pilot.pause()
        assert router.find_cursor_index == 2
        snap = conv._sticky().snapshot()  # type: ignore[union-attr]
        assert snap["active"] is True
        assert "match 2/3" in snap["body"]
        assert "'needle'" in snap["body"]
        assert "line 3" in snap["body"]  # 1-indexed

        # Cycle forward again → idx 4
        router.cycle_find(+1)
        await pilot.pause()
        assert router.find_cursor_index == 4
        snap = conv._sticky().snapshot()  # type: ignore[union-attr]
        assert "match 3/3" in snap["body"]


@pytest.mark.asyncio
async def test_cycle_find_forward_wraps_to_first() -> None:
    """Tier 2: Ctrl+G past the last match wraps back to the first."""
    from reyn.chat.outbox import OutboxMessage
    from reyn.tui.app import ReynTUIApp
    from reyn.tui.app_outbox import OutboxRouter
    from reyn.tui.widgets import ConversationView

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        header = app.query_one("#header")
        _seed_lines(conv, [
            "first match",    # idx 0
            "filler",
            "second match",   # idx 2
        ])
        await pilot.pause()

        router = OutboxRouter(app)
        router._on_find(
            OutboxMessage(kind="__find__", text="match"),
            conv,
            header,
        )
        await pilot.pause()
        assert router.find_cursor_index == 0
        # Forward to last, then forward again → wrap to first
        router.cycle_find(+1)
        await pilot.pause()
        assert router.find_cursor_index == 2
        router.cycle_find(+1)
        await pilot.pause()
        assert router.find_cursor_index == 0
        snap = conv._sticky().snapshot()  # type: ignore[union-attr]
        assert "match 1/2" in snap["body"]


@pytest.mark.asyncio
async def test_cycle_find_backward_steps_and_wraps() -> None:
    """Tier 2: Ctrl+Shift+G steps backward and wraps from first → last."""
    from reyn.chat.outbox import OutboxMessage
    from reyn.tui.app import ReynTUIApp
    from reyn.tui.app_outbox import OutboxRouter
    from reyn.tui.widgets import ConversationView

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        header = app.query_one("#header")
        _seed_lines(conv, [
            "alpha hit",   # idx 0
            "filler",
            "beta hit",    # idx 2
            "filler",
            "gamma hit",   # idx 4
        ])
        await pilot.pause()

        router = OutboxRouter(app)
        router._on_find(
            OutboxMessage(kind="__find__", text="hit"),
            conv,
            header,
        )
        await pilot.pause()
        assert router.find_cursor_index == 0

        # Backward from first → wraps to last (idx 4)
        router.cycle_find(-1)
        await pilot.pause()
        assert router.find_cursor_index == 4
        snap = conv._sticky().snapshot()  # type: ignore[union-attr]
        assert "match 3/3" in snap["body"]

        # Backward again → idx 2 (middle)
        router.cycle_find(-1)
        await pilot.pause()
        assert router.find_cursor_index == 2
        snap = conv._sticky().snapshot()  # type: ignore[union-attr]
        assert "match 2/3" in snap["body"]


@pytest.mark.asyncio
async def test_cycle_find_without_prior_query_shows_usage_hint() -> None:
    """Tier 2: Ctrl+G with no prior /find surfaces a usage hint."""
    from reyn.tui.app import ReynTUIApp
    from reyn.tui.app_outbox import OutboxRouter
    from reyn.tui.widgets import ConversationView

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        # No /find dispatched — cycle should report usage.
        router = OutboxRouter(app)
        assert router.find_query is None
        router.cycle_find(+1)
        await pilot.pause()
        snap = conv._sticky().snapshot()  # type: ignore[union-attr]
        assert snap["active"] is True
        assert "no active /find query" in snap["body"]


@pytest.mark.asyncio
async def test_cycle_find_handles_buffer_mutation_to_zero_matches() -> None:
    """Tier 2: cycle after buffer mutated to drop all matches → no-match status.

    After a successful /find seeds the cycle state, if the user
    then runs Ctrl+L (= clear) or the buffer mutates so the query
    no longer matches anything, cycling must surface the no-match
    branch rather than NoneType-erroring or scrolling to a stale
    line. State is also cleared so a subsequent /find starts fresh.
    """
    from reyn.chat.outbox import OutboxMessage
    from reyn.tui.app import ReynTUIApp
    from reyn.tui.app_outbox import OutboxRouter
    from reyn.tui.widgets import ConversationView

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        header = app.query_one("#header")
        _seed_lines(conv, ["alpha tag", "beta tag"])
        await pilot.pause()

        router = OutboxRouter(app)
        router._on_find(
            OutboxMessage(kind="__find__", text="tag"),
            conv,
            header,
        )
        await pilot.pause()
        assert router.find_query == "tag"
        assert router.find_cursor_index is not None

        # Wipe the buffer — Ctrl+L equivalent.
        conv.clear()
        await pilot.pause()

        router.cycle_find(+1)
        await pilot.pause()
        snap = conv._sticky().snapshot()  # type: ignore[union-attr]
        assert "no matches for 'tag'" in snap["body"]
        # State cleared so a fresh /find isn't shadowed.
        assert router.find_query is None
        assert router.find_cursor_index is None


@pytest.mark.asyncio
async def test_find_next_prev_bindings_registered() -> None:
    """Tier 2: ``ctrl+g`` and ``ctrl+shift+g`` are bound to find actions.

    Pin both bindings + their action names so a rename / removal
    surfaces here. ``action_find_next`` / ``action_find_prev``
    are the app-side entry points that delegate to the router's
    cycle state.
    """
    from reyn.tui.app import ReynTUIApp

    binds = {(b.key, b.action) for b in ReynTUIApp.BINDINGS}
    assert ("ctrl+g", "find_next") in binds
    assert ("ctrl+shift+g", "find_prev") in binds


@pytest.mark.asyncio
async def test_action_find_next_safe_when_router_absent() -> None:
    """Tier 2: ``action_find_next`` is a silent no-op without an active router.

    The router is set by ``_outbox_loop`` (= only when a registry is
    attached). Without a registry the action must not raise — the
    binding still fires from the App's BINDINGS even with no
    backing router instance.
    """
    from reyn.tui.app import ReynTUIApp

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        # Router never started (no registry); attribute is None.
        assert app.outbox_router is None
        # Both actions must be safe no-ops.
        app.action_find_next()
        app.action_find_prev()


@pytest.mark.asyncio
async def test_initial_find_seeds_cycle_state() -> None:
    """Tier 2: ``/find <q>`` seeds router state so Ctrl+G can pick up from there.

    Before the cycle feature, ``_on_find`` left no state behind
    and a subsequent Ctrl+G had no way to know what to step
    through. This pins that the post-/find router state is
    ``(query, cursor_idx)`` where cursor_idx is the first match.
    """
    from reyn.chat.outbox import OutboxMessage
    from reyn.tui.app import ReynTUIApp
    from reyn.tui.app_outbox import OutboxRouter
    from reyn.tui.widgets import ConversationView

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        header = app.query_one("#header")
        _seed_lines(conv, ["pear", "apple", "banana"])
        await pilot.pause()
        router = OutboxRouter(app)
        router._on_find(
            OutboxMessage(kind="__find__", text="apple"),
            conv,
            header,
        )
        await pilot.pause()
        assert router.find_query == "apple"
        assert router.find_cursor_index == 1  # 'apple' is at line idx 1
