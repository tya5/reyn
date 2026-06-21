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


# #2003-class flake-harden: the flash window is wall-clock-driven, so "re-add
# inside the flash window" was a timing race under xdist load (a slowed worker
# let the window close before the re-add, silently degrading the test to a fresh
# add). Drive the window deterministically — a LARGE window keeps it open across
# the re-add; for the "survives the stale timer" test the re-add is SYNCHRONOUS
# (cancels the timer before it can fire) and we poll past a FAST window for a
# (buggy) stale removal. Same shape as #2003; behaviour assertions unchanged.
_FLASH_OPEN_S = 3600.0
_FLASH_FAST_S = 0.05


def _patch_flash(monkeypatch, seconds: float) -> None:
    import reyn.interfaces.tui.widgets.async_stack_panel as _asp
    monkeypatch.setattr(_asp, "_FLASH_DURATION_S", seconds)


async def _wait_until(pilot, predicate, *, cap: float = 2.0, step: float = 0.02) -> bool:
    waited = 0.0
    while waited < cap:
        if predicate():
            return True
        await pilot.pause(step)
        waited += step
    return predicate()


@pytest.mark.asyncio
async def test_readd_during_flash_returns_to_running_state(monkeypatch) -> None:
    """Tier 2: re-adding an id mid-flash clears the interrupted flash state."""
    from reyn.interfaces.tui.app import ReynTUIApp
    from reyn.interfaces.tui.widgets import ConversationView

    _patch_flash(monkeypatch, _FLASH_OPEN_S)  # window stays open across the re-add
    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        panel = app.query_one("#conversation", ConversationView)._async_stack()
        assert panel is not None

        panel.add("a1", "running task")
        await pilot.pause()
        panel.remove("a1", terminal="interrupted")
        await pilot.pause()  # the injected window cannot close during the test

        # Same agent restarts → re-add under the same id, still inside the window.
        panel.add("a1", "restarted task")
        await pilot.pause()

        row = _panel_row(panel.snapshot(), "a1")
        assert row is not None, "re-added row must be present"
        assert row["flashing"] is False, (
            "a re-added (running) row must NOT still be in the interrupted flash "
            "state — add() must reset flashing"
        )


@pytest.mark.asyncio
async def test_readd_during_flash_survives_old_timer(monkeypatch) -> None:
    """Tier 2: the stale flash timer must not yank a re-added live row."""
    from reyn.interfaces.tui.app import ReynTUIApp
    from reyn.interfaces.tui.widgets import ConversationView

    _patch_flash(monkeypatch, _FLASH_FAST_S)  # original timer would fire promptly
    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        panel = app.query_one("#conversation", ConversationView)._async_stack()
        assert panel is not None

        panel.add("a2", "running task")
        await pilot.pause()
        panel.remove("a2", terminal="interrupted")
        # Re-add WHILE the flash timer is still pending — synchronously, before any
        # await lets the timer fire, so the re-add deterministically lands inside
        # the window and must cancel the stale timer (no wall-clock race).
        panel.add("a2", "restarted task")
        await pilot.pause()

        # Poll PAST the original (fast) flash deadline for a (buggy) stale removal:
        # a non-cancelled timer would yank the live row; it must never happen.
        yanked = await _wait_until(
            pilot, lambda: not _panel_has_id(panel.snapshot(), "a2"),
            cap=_FLASH_FAST_S * 6,
        )
        assert not yanked, (
            "the re-added live row must survive past the old flash deadline — "
            "add() must cancel the stale flash timer"
        )
