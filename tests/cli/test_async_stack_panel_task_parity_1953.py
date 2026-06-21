"""Tier 2: #1953 — task-driven decompose drives the AsyncStackPanel bottom strip.

The task-driven ``decompose`` tool's lifecycle renders a bottom-strip
AsyncStackPanel "running" row. The owner-split:

  - EMIT (decompose dispatcher): ``OutboxMessage(kind="system",
    meta={"source": "task_summary"|"task_complete", "parent_task_id": ...})``.
  - RENDER (here): ``OutboxRouter._on_system`` routes ``task_summary`` →
    ``conv.add_async_task`` and ``task_complete`` → ``conv.remove_async_task``,
    keyed on ``parent_task_id``.

These pin the RENDER half against the agreed message shape (the EMIT half lands
in decompose's dispatcher; end-to-end is validated by a real-terminal post-wire
capture). No-mock pattern (real app + real OutboxRouter + recording snapshot).

This is the canonical ``_on_system`` AsyncStackPanel test post-#2018: the plan
lifecycle was clean-break deleted, so ``task_*`` is the only live source. The
generic-system-message guard (last test) absorbed the one unique case from the
retired test_async_stack_panel_plan_lifecycle.py.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


@pytest.mark.asyncio
async def test_task_summary_adds_async_stack_row() -> None:
    """Tier 2: task_summary system msg → AsyncStackPanel row mounted (keyed on parent_task_id)."""
    from reyn.interfaces.tui.app import ReynTUIApp
    from reyn.interfaces.tui.app_outbox import OutboxRouter
    from reyn.interfaces.tui.widgets import ConversationView
    from reyn.runtime.outbox import OutboxMessage

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        router = OutboxRouter(app)
        header = app.query_one("#header")

        msg = OutboxMessage(
            kind="system",
            text="Decomposing:\n1. gather X\n2. gather Y",
            meta={"parent_task_id": "task-abc12345-def6", "source": "task_summary"},
        )
        router._on_system(msg, conv, header)
        await pilot.pause()

        rows = [s for s in conv.async_stack_snapshot() if not s["is_overflow"]]
        assert any(s["agent_id"] == "task-abc12345-def6" for s in rows), (
            f"task row should appear in panel snapshot; got {rows!r}"
        )


@pytest.mark.asyncio
async def test_task_complete_removes_async_stack_row() -> None:
    """Tier 2: task_complete system msg → AsyncStackPanel row dropped."""
    from reyn.interfaces.tui.app import ReynTUIApp
    from reyn.interfaces.tui.app_outbox import OutboxRouter
    from reyn.interfaces.tui.widgets import ConversationView
    from reyn.runtime.outbox import OutboxMessage

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        router = OutboxRouter(app)
        header = app.query_one("#header")

        spawn = OutboxMessage(
            kind="system", text="Decomposing:\n1. step",
            meta={"parent_task_id": "task-zzz999", "source": "task_summary"},
        )
        router._on_system(spawn, conv, header)
        await pilot.pause()
        assert any(
            s["agent_id"] == "task-zzz999"
            for s in conv.async_stack_snapshot() if not s["is_overflow"]
        )

        complete = OutboxMessage(
            kind="system", text="done · task-zzz999",
            meta={"parent_task_id": "task-zzz999", "source": "task_complete"},
        )
        router._on_system(complete, conv, header)
        await pilot.pause()
        assert all(s["agent_id"] != "task-zzz999" for s in conv.async_stack_snapshot())


@pytest.mark.asyncio
async def test_task_summary_missing_parent_task_id_is_silent_noop() -> None:
    """Tier 2b: task_summary without parent_task_id → silent no-op.

    Guard against emit-side shape drift — no row keyed on empty string.
    """
    from reyn.interfaces.tui.app import ReynTUIApp
    from reyn.interfaces.tui.app_outbox import OutboxRouter
    from reyn.interfaces.tui.widgets import ConversationView
    from reyn.runtime.outbox import OutboxMessage

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        router = OutboxRouter(app)
        header = app.query_one("#header")

        msg = OutboxMessage(
            kind="system", text="Decomposing:\n…",
            meta={"source": "task_summary"},  # no parent_task_id
        )
        router._on_system(msg, conv, header)
        await pilot.pause()
        assert conv.async_stack_snapshot() == []


@pytest.mark.asyncio
async def test_non_task_system_message_does_not_touch_async_stack() -> None:
    """Tier 2b: a generic system message (no source, no id — e.g. a
    compaction marker / slash output / attach notice) → no AsyncStackPanel
    side-effect; the default render path still fires. Guards against ghost
    rows from non-task system messages."""
    from reyn.interfaces.tui.app import ReynTUIApp
    from reyn.interfaces.tui.app_outbox import OutboxRouter
    from reyn.interfaces.tui.widgets import ConversationView
    from reyn.runtime.outbox import OutboxMessage

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        router = OutboxRouter(app)
        header = app.query_one("#header")

        # Compaction marker — no parent_task_id, no source.
        msg = OutboxMessage(kind="system", text="[↑ 5 turns compacted]", meta={})
        router._on_system(msg, conv, header)
        await pilot.pause()
        assert conv.async_stack_snapshot() == [], (
            "non-task system message must not touch the AsyncStackPanel"
        )
