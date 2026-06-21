"""Tier 2: async-strip rows show the running skill's live phase (design-check #2).

Before: ``add_async_task(run_id, skill_name)`` set the bottom-strip row summary
once at spawn, so a long-running BACKGROUND skill showed only a ticking elapsed
clock with no signal of WHICH phase it was in (it looked potentially stuck).
Now each ``phase started`` trace refreshes the row summary to ``skill · <phase>``
(+ a plan ``(N/M)`` count when known), updating the existing row in place.

Public surface only: ``_async_strip_summary`` output and
``AsyncStackPanel.snapshot()`` — no private entry dicts asserted.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def _row(snap: "list[dict]", agent_id: str) -> "dict | None":
    for s in snap:
        if s.get("agent_id") == agent_id and not s.get("is_overflow"):
            return s
    return None


@pytest.mark.asyncio
async def test_plan_step_count_appears_in_strip() -> None:
    """Tier 2: a known plan step count surfaces as ``(N/M)`` in the strip summary.

    Drives the real trace stream (``detail: plan N/M`` then a ``phase started``
    that refreshes the row) and asserts the public ``snapshot()`` summary —
    pins the plan-count wiring without touching the private summary builder.
    """
    from reyn.interfaces.tui.app import ReynTUIApp
    from reyn.interfaces.tui.widgets import ConversationView
    from reyn.runtime.outbox import OutboxMessage

    def _msg(text: str) -> OutboxMessage:
        return OutboxMessage(
            kind="trace", text=text,
            meta={"run_id": "r1", "skill_name": "my_skill"},
        )

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        panel = conv._async_stack()

        app._update_skill_exec(_msg("phase started: plan"))
        app._update_skill_exec(_msg("detail: plan 2/5"))
        app._update_skill_exec(_msg("phase started: execute"))
        await pilot.pause()

        row = _row(panel.snapshot(), "r1")
        assert row is not None, "strip row for r1 should be mounted"
        assert "(2/5)" in row["summary"], (
            f"strip summary should surface the plan step count; got {row['summary']!r}"
        )


@pytest.mark.asyncio
async def test_phase_started_shows_phase_in_strip() -> None:
    """Tier 2: a ``phase started`` trace surfaces the phase in the strip summary."""
    from reyn.interfaces.tui.app import ReynTUIApp
    from reyn.interfaces.tui.widgets import ConversationView
    from reyn.runtime.outbox import OutboxMessage

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        panel = conv._async_stack()
        assert panel is not None

        app._update_skill_exec(OutboxMessage(
            kind="trace",
            text="phase started: analyze",
            meta={"run_id": "r1", "skill_name": "my_skill"},
        ))
        await pilot.pause()

        row = _row(panel.snapshot(), "r1")
        assert row is not None, "strip row for r1 should be mounted"
        assert "analyze" in row["summary"], (
            f"strip summary should surface the live phase; got {row['summary']!r}"
        )
        assert "my_skill" in row["summary"], (
            f"strip summary should keep the skill name; got {row['summary']!r}"
        )


@pytest.mark.asyncio
async def test_phase_progression_replaces_phase_in_place() -> None:
    """Tier 2: a later phase replaces the earlier one in the row (live progress).

    The entry is keyed by run_id, so the update is in place; the visible
    invariant is that the summary tracks the CURRENT phase and drops the prior
    one — a long-running task no longer looks frozen on its first phase.
    """
    from reyn.interfaces.tui.app import ReynTUIApp
    from reyn.interfaces.tui.widgets import ConversationView
    from reyn.runtime.outbox import OutboxMessage

    def _trace(phase: str) -> OutboxMessage:
        return OutboxMessage(
            kind="trace", text=f"phase started: {phase}",
            meta={"run_id": "r1", "skill_name": "my_skill"},
        )

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        panel = conv._async_stack()

        app._update_skill_exec(_trace("plan"))
        await pilot.pause()
        app._update_skill_exec(_trace("execute"))
        await pilot.pause()

        row = _row(panel.snapshot(), "r1")
        assert row is not None, "strip row for r1 should still be present"
        assert "execute" in row["summary"], (
            f"strip should show the current phase; got {row['summary']!r}"
        )
        assert "plan" not in row["summary"], (
            f"stale earlier phase leaked into the summary; got {row['summary']!r}"
        )
