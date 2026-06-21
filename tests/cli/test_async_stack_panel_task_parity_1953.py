"""Tier 2: #1953 P3 (b) — task-driven decompose drives AsyncStackPanel at parity with plan.

The task-driven ``decompose`` tool is the analog of ``plan``. For TUI-surface
parity (the P4 delete-gate's TUI half), its lifecycle must render the SAME
bottom-strip AsyncStackPanel "running" row as plan. The agreed owner-split:

  - EMIT (decompose dispatcher): ``OutboxMessage(kind="system",
    meta={"source": "task_summary"|"task_complete", "parent_task_id": ...})`` —
    mirrors plan_runner's ``plan_summary``/``plan_complete`` (keyed on plan_id).
  - RENDER (here): ``OutboxRouter._on_system`` routes ``task_summary`` →
    ``conv.add_async_task`` and ``task_complete`` → ``conv.remove_async_task``,
    keyed on ``parent_task_id`` — reusing the exact plan render path.

These pin the RENDER half against the agreed message shape (the EMIT half lands
in decompose's dispatcher; end-to-end is validated by a real-terminal post-wire
capture). Same no-mock pattern as test_async_stack_panel_plan_lifecycle.py.
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
            f"task row should appear in panel snapshot (parity with plan); got {rows!r}"
        )


@pytest.mark.asyncio
async def test_task_complete_removes_async_stack_row() -> None:
    """Tier 2: task_complete system msg → AsyncStackPanel row dropped (parity with plan_complete)."""
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
    """Tier 2b: task_summary without parent_task_id (and no plan_id) → silent no-op.

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
            meta={"source": "task_summary"},  # no parent_task_id, no plan_id
        )
        router._on_system(msg, conv, header)
        await pilot.pause()
        assert conv.async_stack_snapshot() == []
