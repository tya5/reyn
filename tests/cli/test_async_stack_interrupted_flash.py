"""Tier 2: AsyncStackPanel interrupted/aborted flash before unmount (W13 T2-2).

Covers audit finding A#6: clean completion vs abort vs interrupt were
visually identical (= row vanishes). The new ``terminal`` parameter on
``AsyncStackPanel.remove`` gives non-ok terminations a brief red flash
before unmounting.

Public surfaces tested (per testing policy — no private state assertions):
  1. ``remove("p1", terminal="ok")`` → row immediately gone (= existing
     behaviour preserved; backwards-compat).
  2. ``remove("p2", terminal="interrupted")`` → row text contains
     "interrupted" AND row still in DOM (= flashing=True) after 0.1 s.
  3. After ~1.6 s (= post-flush), row is gone from DOM.
  4. Calling ``remove("p3", terminal="interrupted")`` then
     ``remove("p3", terminal="ok")`` quickly → row gone immediately
     (= second remove cancels pending timer, unmounts immediately).
  5. Backwards-compat: existing ``remove("p4")`` no-kwarg call still
     works (= ``terminal`` defaults to ``"ok"``).

Timer determinism: tests 3 uses ``asyncio.sleep(1.6)`` via pilot.pause;
test 4 verifies immediate cancellation without sleeping.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


# ── helpers ──────────────────────────────────────────────────────────────────

def _panel_has_id(snap: list[dict], agent_id: str) -> bool:
    """Return True when ``agent_id`` appears in the snapshot (non-overflow rows)."""
    return any(s["agent_id"] == agent_id for s in snap if not s["is_overflow"])


def _panel_row(snap: list[dict], agent_id: str) -> dict | None:
    """Return the snapshot dict for ``agent_id``, or None if absent."""
    for s in snap:
        if s["agent_id"] == agent_id and not s["is_overflow"]:
            return s
    return None


# ── test 1: terminal="ok" → immediate unmount ────────────────────────────────

@pytest.mark.asyncio
async def test_remove_ok_unmounts_immediately() -> None:
    """Tier 2: remove(terminal="ok") drops row immediately — existing behaviour."""
    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets import ConversationView

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        panel = conv._async_stack()
        assert panel is not None

        panel.add("p1", "doing stuff")
        await pilot.pause()
        assert _panel_has_id(panel.snapshot(), "p1"), "row must appear after add"

        panel.remove("p1", terminal="ok")
        await pilot.pause()
        assert not _panel_has_id(panel.snapshot(), "p1"), (
            "row must be gone immediately after terminal='ok'"
        )


# ── test 2: terminal="interrupted" → row stays (flashing) for 0.1 s ─────────

@pytest.mark.asyncio
async def test_remove_interrupted_row_still_present_before_flush() -> None:
    """Tier 2: remove(terminal="interrupted") → row present and flashing after 0.1 s."""
    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets import ConversationView

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        panel = conv._async_stack()
        assert panel is not None

        panel.add("p2", "long running task")
        await pilot.pause()
        assert _panel_has_id(panel.snapshot(), "p2"), "row must appear after add"

        panel.remove("p2", terminal="interrupted")
        await pilot.pause(0.1)  # 0.1 s — well before the 1.5 s flush

        snap = panel.snapshot()
        row = _panel_row(snap, "p2")
        assert row is not None, (
            "row must still be present during the flash window (before 1.5 s flush)"
        )
        assert row["flashing"] is True, (
            "snapshot must report flashing=True during the flash window"
        )
        # The rendered text (via rendered_text()) should contain "interrupted"
        rendered = panel._build_lines().plain
        assert "interrupted" in rendered, (
            f"rendered text must contain 'interrupted' during flash; got: {rendered!r}"
        )


# ── test 3: terminal="interrupted" → row gone after 1.6 s ───────────────────

@pytest.mark.asyncio
async def test_remove_interrupted_row_gone_after_flush_delay() -> None:
    """Tier 2: remove(terminal="interrupted") → row gone after ~1.6 s (post-flush)."""
    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets import ConversationView

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        panel = conv._async_stack()
        assert panel is not None

        panel.add("p2b", "a background plan")
        await pilot.pause()

        panel.remove("p2b", terminal="interrupted")
        await pilot.pause(1.6)  # past the 1.5 s flash window

        assert not _panel_has_id(panel.snapshot(), "p2b"), (
            "row must be gone from DOM after the 1.5 s flash window expires"
        )


# ── test 4: second remove() cancels pending timer + unmounts immediately ─────

@pytest.mark.asyncio
async def test_second_remove_cancels_timer_and_unmounts_immediately() -> None:
    """Tier 2: remove(interrupted) then remove(ok) → immediate unmount (timer cancelled)."""
    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets import ConversationView

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        panel = conv._async_stack()
        assert panel is not None

        panel.add("p3", "concurrent task")
        await pilot.pause()

        # First remove — enters flash window, arms timer
        panel.remove("p3", terminal="interrupted")
        await pilot.pause()
        assert _panel_has_id(panel.snapshot(), "p3"), (
            "row must still be present immediately after terminal='interrupted'"
        )

        # Second remove — should cancel the timer and unmount immediately
        panel.remove("p3", terminal="ok")
        await pilot.pause()
        assert not _panel_has_id(panel.snapshot(), "p3"), (
            "row must be gone immediately after second remove() cancels the flash timer"
        )


# ── test 5: no-kwarg call still works (backwards-compat) ─────────────────────

@pytest.mark.asyncio
async def test_remove_no_kwarg_still_works() -> None:
    """Tier 2: remove(agent_id) no-kwarg call → immediate unmount (backwards-compat)."""
    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets import ConversationView

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        panel = conv._async_stack()
        assert panel is not None

        panel.add("p4", "some task")
        await pilot.pause()
        assert _panel_has_id(panel.snapshot(), "p4"), "row must appear after add"

        # No terminal kwarg — must default to "ok" (= immediate unmount)
        panel.remove("p4")
        await pilot.pause()
        assert not _panel_has_id(panel.snapshot(), "p4"), (
            "no-kwarg remove() must still unmount immediately (backwards-compat)"
        )


# ── test 6: elapsed freezes at flash start (not live) ────────────────────────

@pytest.mark.asyncio
async def test_elapsed_frozen_at_flash_start() -> None:
    """Tier 2: elapsed_s in snapshot is frozen the moment remove(terminal!='ok') fires.

    After calling remove(terminal='aborted'), the snapshot's elapsed_s for
    the flashing row must not increase when time passes — it must stay at
    (approximately) the value captured at the moment of remove().
    """
    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets import ConversationView

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        panel = conv._async_stack()
        assert panel is not None

        panel.add("p5", "background job")
        await pilot.pause()

        # Let 0.2 s accumulate so elapsed_s is non-trivial at remove time.
        await pilot.pause(0.2)

        panel.remove("p5", terminal="aborted")
        await pilot.pause()

        # Snapshot immediately after remove — capture the frozen elapsed.
        snap1 = panel.snapshot()
        row1 = _panel_row(snap1, "p5")
        assert row1 is not None, "row must still be present during flash window"
        assert row1["flashing"] is True
        elapsed_at_flash = row1["elapsed_s"]

        # Wait ~1 s — well inside the 1.5 s flash window.
        await pilot.pause(1.0)

        snap2 = panel.snapshot()
        row2 = _panel_row(snap2, "p5")
        assert row2 is not None, "row must still be present within the flash window"
        assert row2["flashing"] is True

        # Elapsed must be frozen: same value as at flash start (within float noise).
        assert row2["elapsed_s"] == elapsed_at_flash, (
            f"elapsed_s must be frozen during flash; "
            f"at flash start: {elapsed_at_flash}, after 1 s: {row2['elapsed_s']}"
        )


# ── test 7: non-flashing entry continues to update elapsed ───────────────────

@pytest.mark.asyncio
async def test_live_elapsed_for_running_entry() -> None:
    """Tier 2: elapsed_s in snapshot increases for a non-flashing (running) entry.

    Freeze logic must only apply to flashing entries; a still-running entry
    must continue to report live elapsed so the user sees the task progressing.
    """
    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets import ConversationView

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        panel = conv._async_stack()
        assert panel is not None

        panel.add("p6", "still running task")
        await pilot.pause()

        snap1 = panel.snapshot()
        row1 = _panel_row(snap1, "p6")
        assert row1 is not None
        assert row1["flashing"] is False
        elapsed1 = row1["elapsed_s"]

        # Wait 1 s — elapsed must grow for a running entry.
        await pilot.pause(1.0)

        snap2 = panel.snapshot()
        row2 = _panel_row(snap2, "p6")
        assert row2 is not None
        assert row2["flashing"] is False

        assert row2["elapsed_s"] > elapsed1, (
            f"live elapsed must increase for running entry; "
            f"before: {elapsed1}, after 1 s: {row2['elapsed_s']}"
        )

        # Cleanup: remove cleanly so no timers dangle.
        panel.remove("p6", terminal="ok")
        await pilot.pause()
