"""Tier 2: plan lifecycle drives AsyncStackPanel via system outbox messages.

Follow-up to the I-F5 wiring PR. Plan spawns + completions reach the
TUI as ``OutboxMessage(kind="system", meta={"source": ..., "plan_id":
...})`` emitted by ``plan_runner``:

  - ``source="plan_summary"`` (= plan started, ``[task_spawned]
    kind=plan`` equivalent)
  - ``source="plan_complete"`` (= plan finished, ``[task_completed]``
    equivalent)

The new ``OutboxRouter._on_system`` handler intercepts those two
sources and routes them to ``conv.add_async_task`` /
``conv.remove_async_task`` (= the same helpers the skill lifecycle
wiring uses). Other ``kind="system"`` messages flow through the
default ``conv.render_message`` path unchanged.

Public surfaces tested:
  - plan_summary → row added (= keyed on plan_id)
  - plan_complete → row removed
  - non-plan system message → no effect on AsyncStackPanel,
    default render still fires
  - missing plan_id → silent no-op (= defensive against ill-shaped
    outbox messages)
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


@pytest.mark.asyncio
async def test_plan_summary_adds_async_stack_row() -> None:
    """Tier 2: plan_summary system msg → AsyncStackPanel row mounted."""
    from reyn.chat.outbox import OutboxMessage
    from reyn.tui.app import ReynTUIApp
    from reyn.tui.app_outbox import OutboxRouter
    from reyn.tui.widgets import ConversationView

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        router = OutboxRouter(app)
        header = app.query_one("#header")

        msg = OutboxMessage(
            kind="system",
            text="Executing plan:\n1. analyse\n2. summarise",
            meta={"plan_id": "plan-abc12345-def6", "source": "plan_summary"},
        )
        router._on_system(msg, conv, header)
        await pilot.pause()

        snap = conv.async_stack_snapshot()
        rows = [s for s in snap if not s["is_overflow"]]
        assert any(
            s["agent_id"] == "plan-abc12345-def6" for s in rows
        ), f"plan row should appear in panel snapshot; got {rows!r}"


@pytest.mark.asyncio
async def test_plan_complete_removes_async_stack_row() -> None:
    """Tier 2: plan_complete system msg → AsyncStackPanel row dropped."""
    from reyn.chat.outbox import OutboxMessage
    from reyn.tui.app import ReynTUIApp
    from reyn.tui.app_outbox import OutboxRouter
    from reyn.tui.widgets import ConversationView

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        router = OutboxRouter(app)
        header = app.query_one("#header")

        # Spawn first.
        spawn = OutboxMessage(
            kind="system",
            text="Executing plan:\n1. step",
            meta={"plan_id": "plan-zzz999", "source": "plan_summary"},
        )
        router._on_system(spawn, conv, header)
        await pilot.pause()
        assert any(
            s["agent_id"] == "plan-zzz999"
            for s in conv.async_stack_snapshot()
            if not s["is_overflow"]
        )

        # Then complete.
        complete = OutboxMessage(
            kind="system",
            text="plan complete: 1/1 steps succeeded · plan-zzz999",
            meta={"plan_id": "plan-zzz999", "source": "plan_complete"},
        )
        router._on_system(complete, conv, header)
        await pilot.pause()
        assert all(
            s["agent_id"] != "plan-zzz999"
            for s in conv.async_stack_snapshot()
        )


@pytest.mark.asyncio
async def test_non_plan_system_message_does_not_touch_async_stack() -> None:
    """Tier 2b: generic system message → no AsyncStackPanel side-effect.

    Lifecycle markers (= ``[↑ N turns compacted]``), slash-command
    output, attach/detach notices are all ``kind="system"`` but
    carry no plan_id. They must NOT add ghost rows to the panel.
    """
    from reyn.chat.outbox import OutboxMessage
    from reyn.tui.app import ReynTUIApp
    from reyn.tui.app_outbox import OutboxRouter
    from reyn.tui.widgets import ConversationView

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        router = OutboxRouter(app)
        header = app.query_one("#header")

        # Compaction marker — no plan_id, no source.
        msg = OutboxMessage(
            kind="system",
            text="[↑ 5 turns compacted]",
            meta={},
        )
        router._on_system(msg, conv, header)
        await pilot.pause()

        assert conv.async_stack_snapshot() == [], (
            "non-plan system message must not touch the AsyncStackPanel"
        )


@pytest.mark.asyncio
async def test_missing_plan_id_is_silent_noop() -> None:
    """Tier 2b: plan_summary without plan_id meta → silent no-op.

    Guard against producer-side shape drift — if the outbox emitter
    drops the plan_id for some reason, the handler should silently
    skip the panel side-effect rather than mount a row keyed by
    empty string.
    """
    from reyn.chat.outbox import OutboxMessage
    from reyn.tui.app import ReynTUIApp
    from reyn.tui.app_outbox import OutboxRouter
    from reyn.tui.widgets import ConversationView

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        router = OutboxRouter(app)
        header = app.query_one("#header")

        msg = OutboxMessage(
            kind="system",
            text="Executing plan:\n…",
            meta={"source": "plan_summary"},  # no plan_id
        )
        router._on_system(msg, conv, header)
        await pilot.pause()
        # Panel stays empty — no row keyed on empty string.
        assert conv.async_stack_snapshot() == []
