"""Tier 2: AsyncStackPanel is mounted + wired to attached-agent task lifecycle.

Wave-10 follow-up I-F5 (= honest scope drop revisited): the
``AsyncStackPanel`` widget existed as a PoC but was never mounted
into the conv pane and had no event source driving it. User
direction (2026-05-23) narrowed the scope to attached-agent
tasks only.

This PR mounts the panel in ``ConversationView.compose()`` near
the StickyStatus dock and wires the production hooks:

  - ``add_async_task(run_id, skill_name)`` fires on the FIRST
    trace for a new ``run_id`` (= the
    ``[task_spawned] kind=skill`` boundary)
  - ``remove_async_task(run_id)`` fires on the ``"skill done: …"``
    trace AND on the local + remote Ctrl+C cancel paths (=
    ``[task_completed]`` boundary or its cancel equivalent)
  - ``clear_async_tasks()`` fires on ``ConversationView.clear()``
    (= Ctrl+L wipes the running-task overview alongside the log)

Public surfaces tested:
  - panel is present in the mounted DOM
  - add_async_task → entry visible in the panel's snapshot
  - remove_async_task → entry gone
  - clear_async_tasks → panel empty
  - silent no-op when panel is somehow absent (= defensive
    against ConversationView used outside the standard compose
    path)
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


@pytest.mark.asyncio
async def test_async_stack_panel_mounted_in_conv_pane() -> None:
    """Tier 2: ``compose()`` yields an AsyncStackPanel with id ``async-stack``."""
    from reyn.tui.app import ReynTUIApp
    from reyn.tui.widgets import ConversationView
    from reyn.tui.widgets.async_stack_panel import AsyncStackPanel

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        panel = conv.query_one("#async-stack", AsyncStackPanel)
        assert panel is not None


@pytest.mark.asyncio
async def test_add_async_task_surfaces_entry_in_snapshot() -> None:
    """Tier 2: ``add_async_task`` adds a row visible via the panel's snapshot."""
    from reyn.tui.app import ReynTUIApp
    from reyn.tui.widgets import ConversationView

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        conv.add_async_task("run-abc", "code_review")
        await pilot.pause()
        snap = conv.async_stack_snapshot()
        # First non-overflow entry should be our task.
        running = [s for s in snap if not s["is_overflow"]]
        assert running, "task should appear in panel snapshot"
        assert running[0]["agent_id"] == "run-abc"
        assert running[0]["summary"] == "code_review"


@pytest.mark.asyncio
async def test_remove_async_task_drops_entry() -> None:
    """Tier 2: ``remove_async_task`` removes the entry from the panel."""
    from reyn.tui.app import ReynTUIApp
    from reyn.tui.widgets import ConversationView

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        conv.add_async_task("run-xyz", "shell")
        await pilot.pause()
        assert any(
            s["agent_id"] == "run-xyz"
            for s in conv.async_stack_snapshot()
        )
        conv.remove_async_task("run-xyz")
        await pilot.pause()
        assert all(
            s["agent_id"] != "run-xyz"
            for s in conv.async_stack_snapshot()
        )


@pytest.mark.asyncio
async def test_clear_async_tasks_empties_panel() -> None:
    """Tier 2: ``clear_async_tasks`` (= Ctrl+L path) leaves the panel empty.

    The full ``ConversationView.clear()`` also calls this — verified
    indirectly via the panel ending up empty after clear.
    """
    from reyn.tui.app import ReynTUIApp
    from reyn.tui.widgets import ConversationView

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        conv.add_async_task("run-1", "a")
        conv.add_async_task("run-2", "b")
        await pilot.pause()
        assert len(conv.async_stack_snapshot()) >= 2
        conv.clear()
        await pilot.pause()
        assert conv.async_stack_snapshot() == []


@pytest.mark.asyncio
async def test_helpers_safe_when_panel_absent() -> None:
    """Tier 2b: ``add/remove/clear_async_tasks`` are safe no-ops (defensive).

    ``_async_stack()`` returns None when the panel isn't mounted
    (= ConversationView used outside the standard compose path,
    or panel removed mid-test). The public helper methods must
    silently no-op in that case rather than raise.

    Simulated by monkey-patching ``_async_stack`` to return None.
    Directly unmounting via ``panel.remove()`` would collide with
    AsyncStackPanel's own ``remove(agent_id)`` method override.
    """
    from reyn.tui.app import ReynTUIApp
    from reyn.tui.widgets import ConversationView

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        conv._async_stack = lambda: None  # type: ignore[method-assign]
        # Helpers must not raise.
        conv.add_async_task("run-x", "foo")
        conv.remove_async_task("run-x")
        conv.clear_async_tasks()
