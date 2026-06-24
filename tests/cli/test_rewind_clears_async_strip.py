"""Tier 2: a rewind checkout clears the bottom async strip (orphan-fix).

Dogfood-found (live `/rewind`): after a checkout that resets the agent + cancels
in-flight work, the AsyncStackPanel kept the pre-rewind skill-run rows shown as
⟳-in-flight forever (growing elapsed) — their completion no longer routed to the
strip because the rewind orphaned the tracking. `_do_checkout` now reconciles by
clearing the strip (same as the Ctrl+L / ConversationView.clear path).

Real `ReynTUIApp` + real `ConversationView` + a real ``checkout``-shaped registry
double (no mocks); asserts via the public ``async_stack_snapshot()`` surface.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


class _CheckoutRegistry:
    """Real registry double exposing only the ``checkout`` _do_checkout calls."""

    def __init__(self) -> None:
        self.calls: list[int] = []

    async def checkout(self, seq: int) -> dict:
        self.calls.append(seq)
        return {"agents": ["t"], "target_n": seq}


@pytest.mark.asyncio
async def test_rewind_checkout_clears_async_strip() -> None:
    """Tier 2: ``_do_checkout`` clears the async strip so pre-rewind in-flight
    rows don't hang as ⟳-forever after the agent reset."""
    from reyn.interfaces.tui.app import ReynTUIApp
    from reyn.interfaces.tui.widgets import ConversationView

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        conv.add_async_task("run-1", "direct_llm")
        conv.add_async_task("run-2", "direct_llm")
        await pilot.pause()
        running = [s for s in conv.async_stack_snapshot() if not s["is_overflow"]]
        assert running, "tasks should be present in the strip pre-rewind"

        # The rewind path reads self._agent_registry; inject the checkout double.
        app._agent_registry = _CheckoutRegistry()
        await app._do_checkout(6)
        await pilot.pause()

        assert conv.async_stack_snapshot() == [], (
            "a rewind checkout must clear the orphaned async-strip rows"
        )
