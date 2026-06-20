"""Tier 2: AsyncStackPanel re-add during the interrupt flash window.

Bug: ``remove(agent_id, terminal="interrupted")`` arms a ~1.5 s flash timer
that, on fire, deletes ``agent_id``'s entry. If the SAME ``agent_id`` is
re-added via ``add()`` during that window (= the async/agent restarts, or a
plan is resumed under the same id), the stale flash timer was neither
cancelled nor the entry's ``flashing`` state reset. Consequences:

  1. The freshly re-added (running) row renders as ``✗ ... (interrupted)``
     because ``add()`` left ``flashing=True`` / ``frozen_elapsed_s`` set.
  2. ~1.5 s later the stale timer fires and yanks the LIVE re-added row out
     of the panel — a ghost removal of a running entry.

These tests pin the correct behaviour: a re-add returns the entry to the
running (non-flashing) state AND survives past the old flash deadline.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def _panel_has_id(snap: list[dict], agent_id: str) -> bool:
    return any(s["agent_id"] == agent_id for s in snap if not s["is_overflow"])


def _panel_row(snap: list[dict], agent_id: str) -> dict | None:
    for s in snap:
        if s["agent_id"] == agent_id and not s["is_overflow"]:
            return s
    return None


@pytest.mark.asyncio
async def test_readd_during_flash_returns_to_running_state() -> None:
    """Tier 2: re-adding an id mid-flash clears the interrupted flash state."""
    from reyn.interfaces.tui.app import ReynTUIApp
    from reyn.interfaces.tui.widgets import ConversationView

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        panel = app.query_one("#conversation", ConversationView)._async_stack()
        assert panel is not None

        panel.add("a1", "running task")
        await pilot.pause()
        panel.remove("a1", terminal="interrupted")
        await pilot.pause(0.1)  # inside the flash window

        # Same agent restarts → re-add under the same id.
        panel.add("a1", "restarted task")
        await pilot.pause()

        row = _panel_row(panel.snapshot(), "a1")
        assert row is not None, "re-added row must be present"
        assert row["flashing"] is False, (
            "a re-added (running) row must NOT still be in the interrupted flash "
            "state — add() must reset flashing"
        )


@pytest.mark.asyncio
async def test_readd_during_flash_survives_old_timer() -> None:
    """Tier 2: the stale flash timer must not yank a re-added live row."""
    from reyn.interfaces.tui.app import ReynTUIApp
    from reyn.interfaces.tui.widgets import ConversationView

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        panel = app.query_one("#conversation", ConversationView)._async_stack()
        assert panel is not None

        panel.add("a2", "running task")
        await pilot.pause()
        panel.remove("a2", terminal="interrupted")
        await pilot.pause(0.1)  # inside the flash window

        panel.add("a2", "restarted task")
        await pilot.pause()

        # Past the ORIGINAL ~1.5 s flash deadline.
        await pilot.pause(1.6)

        assert _panel_has_id(panel.snapshot(), "a2"), (
            "the re-added live row must survive past the old flash deadline — "
            "add() must cancel the stale flash timer"
        )
