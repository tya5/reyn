"""Tier 2: Esc-Esc double-tap opens the /rewind picker (#1546).

A *truly clean* Esc (nothing dismissable: no recording / rewind-menu / panel /
InputBar slash-entry) is a no-op today, so it's repurposed as the rewind
double-tap trigger: two clean Escs within ``_ESC_ESC_WINDOW_S`` open the picker.

Pins (real key path through bindings + check_action, run_test pilot — no mocks):
- ``InputBar.has_slash_entry()`` reflects the buffer state.
- check_action grabs a truly-clean Esc but NOT one InputBar needs for slash-entry
  dismissal (the stealing-safety guarantee — tui-coder Q3).
- first clean Esc arms (pending) without opening; second within window opens.
- a second Esc *outside* the window re-arms instead of opening.
- any Esc that dismisses something resets the pending first-tap (reset discipline).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import reyn.tui.app as app_mod
from reyn.tui.app import ReynTUIApp
from reyn.tui.widgets import ConversationView, InputBar


@pytest.fixture(autouse=True)
def _wide_esc_window(monkeypatch):
    """De-flake (#1587): make the Esc-Esc window huge so the real auto-clear
    ``set_timer`` never fires during a (sub-second) test — removing the
    wall-clock race that let a slow 3.11 run zero the pending state mid-test.
    The production window behaviour is unchanged (same monotonic-ts logic, just
    a value the test controls); tests that need the window to *lapse* advance a
    faked ``_now_monotonic`` past it instead of sleeping real time."""
    monkeypatch.setattr(app_mod, "_ESC_ESC_WINDOW_S", 1_000_000.0)


def _make_app() -> ReynTUIApp:
    return ReynTUIApp(
        registry=None, agent_name="test-agent", model="test-model",
        budget_tracker=None,
    )


def _conv_has(app: ReynTUIApp, needle: str) -> bool:
    conv = app.query_one("#conversation", ConversationView)
    return any(needle in line for line in conv.dump_buffer_text())


@pytest.mark.asyncio
async def test_has_slash_entry_reflects_buffer() -> None:
    """Tier 2: has_slash_entry() True while a slash-prefix is in the buffer."""
    app = _make_app()
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        bar = app.query_one("#inputbar", InputBar)
        assert bar.has_slash_entry() is False
        await pilot.press("/", "f", "o", "o")
        await pilot.pause()
        assert bar.has_slash_entry() is True


@pytest.mark.asyncio
async def test_check_action_grabs_clean_esc_but_not_slash_entry() -> None:
    """Tier 2: trap-safe gate — the App grabs a truly-clean Esc (for double-tap
    detection) but yields the Esc to InputBar when a slash-entry is present, so
    InputBar's own binding clears the prefix (tui-coder Q3 stealing-safety)."""
    app = _make_app()
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        # Empty buffer, nothing pending → clean Esc grabbed by the App.
        assert app.check_action("voice_cancel", ()) is True
        # Slash-entry present → App must NOT grab it (InputBar clears prefix).
        await pilot.press("/", "f", "o", "o")
        await pilot.pause()
        assert app.check_action("voice_cancel", ()) is False


@pytest.mark.asyncio
async def test_first_clean_esc_arms_without_opening() -> None:
    """Tier 2: one clean Esc arms the double-tap (pending) but does NOT open."""
    app = _make_app()
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert app.esc_esc_pending is True
        # registry=None → opening would render "no checkpoints"; it must NOT yet.
        assert not _conv_has(app, "no checkpoints")
        assert app.rewind_menu_open is False


@pytest.mark.asyncio
async def test_double_esc_within_window_opens() -> None:
    """Tier 2: two clean Escs within the window reach the picker open path
    (registry=None → the "no checkpoints" notice proves _open_rewind_menu ran)
    and clear the pending first-tap."""
    app = _make_app()
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        await pilot.press("escape")
        await pilot.press("escape")  # back-to-back ≪ window
        await pilot.pause()
        assert _conv_has(app, "no checkpoints")
        assert app.esc_esc_pending is False


@pytest.mark.asyncio
async def test_double_esc_outside_window_rearms(monkeypatch) -> None:
    """Tier 2: a second Esc AFTER the window re-arms (new first tap), not open.

    Deterministic clock (#1587): the window lapse is simulated by advancing a
    faked ``_now_monotonic`` past the window — no real ``asyncio.sleep`` (which
    raced 3.11's timer scheduling at the old +0.05 margin)."""
    clock = {"t": 1000.0}
    monkeypatch.setattr(app_mod, "_now_monotonic", lambda: clock["t"])
    app = _make_app()
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        await pilot.press("escape")          # arm at t=1000
        assert app.esc_esc_pending is True
        # Advance PAST the window deterministically (no wall-clock sleep).
        clock["t"] += app_mod._ESC_ESC_WINDOW_S + 1.0
        await pilot.press("escape")          # 2nd Esc now lands outside the window
        await pilot.pause()
        # Did not double-tap → no open attempt, but re-armed for a fresh pair.
        assert not _conv_has(app, "no checkpoints")
        assert app.esc_esc_pending is True


@pytest.mark.asyncio
async def test_slash_clear_esc_resets_pending() -> None:
    """Tier 2: a slash-prefix-clearing Esc resets the pending first-tap.

    Regression for the tui-coder #1554 repro: `Esc(arm) → /x → Esc(clear slash)
    → Esc(clean)` must NOT false-fire the picker. The slash-clearing Esc is
    consumed by InputBar, so check_action's slash-entry branch is the only place
    that can disarm the first-tap — without that reset, the 3rd Esc lands inside
    the still-open window and wrongly opens the picker.
    """
    app = _make_app()
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        await pilot.press("escape")              # arm
        assert app.esc_esc_pending is True
        await pilot.press("/", "x")              # slash-entry appears
        await pilot.pause()
        await pilot.press("escape")              # clears the slash prefix
        await pilot.pause()
        # The slash-clearing Esc must have disarmed the first-tap.
        assert app.esc_esc_pending is False
        await pilot.press("escape")              # now a fresh clean Esc
        await pilot.pause()
        # It re-arms (new first tap) rather than completing a false double-tap.
        assert not _conv_has(app, "no checkpoints")
        assert app.esc_esc_pending is True


@pytest.mark.asyncio
async def test_dismiss_resets_pending() -> None:
    """Tier 2: an Esc that dismisses something resets the pending first-tap, so
    "dismiss then clean-Esc" can't masquerade as a double-tap (reset discipline)."""
    app = _make_app()
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        await pilot.press("escape")          # arm
        assert app.esc_esc_pending is True
        app.action_toggle_panel()            # make the panel dismissable
        await pilot.pause()
        await pilot.press("escape")          # this Esc dismisses the panel
        await pilot.pause()
        assert app.esc_esc_pending is False   # reset, not advanced to open
        assert not _conv_has(app, "no checkpoints")
