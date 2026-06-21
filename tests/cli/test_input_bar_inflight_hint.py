"""Tier 2: InputBar hint footer swaps to blocked-state text while in-flight (B3).

B3 gap: while an LLM turn is in-flight, pressing Enter is silently
swallowed (correct guard behavior). The only visual feedback was a subtle
CSS dim on the TextArea. The footer hint kept reading "Enter send", which
was misleading — users pressing Enter have no idea they're blocked, not
that the UI is broken.

Fix: ``set_in_flight(True)`` now also updates the hint label to
``_HINT_IN_FLIGHT`` (``⟳ responding — Ctrl+C to cancel``). Clearing
the lock reverts to the normal ``_build_hint`` output.

Public surfaces tested:
  - ``set_in_flight(True)`` updates the live ``#hints`` Label text (full
    Textual harness)
  - ``set_in_flight(False)`` reverts the Label to normal hint text
  - CSS class ``in-flight`` is still added/removed (no regression)
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


# ── live label update: full Textual harness ──────────────────────────────────


def _label_text(label) -> str:
    """Read the live content from a Textual Label/Static widget.

    Uses the public ``content`` property (which tracks ``update()`` calls)
    and converts to str for plain-text assertions. Avoids the private
    ``_Static__content`` mangled name and the version-specific
    ``renderable`` attribute that was removed in newer Textual releases.
    """
    return str(label.content)


@pytest.mark.asyncio
async def test_set_in_flight_true_updates_hint_label() -> None:
    """Tier 2: ``set_in_flight(True)`` swaps the ``#hints`` Label to blocked text.

    Drives a live Textual app so the Label.update() path is exercised
    end-to-end. Asserts on the rendered label text (= public surface),
    not the private ``_in_flight`` flag.
    """
    from textual.widgets import Label

    from reyn.interfaces.tui.app import ReynTUIApp
    from reyn.interfaces.tui.widgets import InputBar

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        bar = app.query_one("#inputbar", InputBar)
        label = app.query_one("#hints", Label)

        # Sanity: starts with normal hint.
        normal_text = _label_text(label)
        assert "Enter send" in normal_text or "Enter" in normal_text, (
            f"initial hint unexpected: {normal_text!r}"
        )

        bar.set_in_flight(True)
        await pilot.pause()

        inflight_text = _label_text(label)
        assert "responding" in inflight_text, (
            f"after set_in_flight(True), hint should show 'responding'; got: {inflight_text!r}"
        )
        assert "Ctrl+C" in inflight_text, (
            f"in-flight hint must show Ctrl+C escape; got: {inflight_text!r}"
        )
        # CSS class is still applied (no regression on the visual dim).
        assert bar.has_class("in-flight")


@pytest.mark.asyncio
async def test_set_in_flight_false_reverts_hint_label() -> None:
    """Tier 2: ``set_in_flight(False)`` reverts the ``#hints`` Label to normal hint.

    Sequence: set True → set False → assert normal hint is back.
    """
    from textual.widgets import Label

    from reyn.interfaces.tui.app import ReynTUIApp
    from reyn.interfaces.tui.widgets import InputBar

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        bar = app.query_one("#inputbar", InputBar)
        label = app.query_one("#hints", Label)

        bar.set_in_flight(True)
        await pilot.pause()
        # Confirm it flipped.
        assert "responding" in _label_text(label)

        bar.set_in_flight(False)
        await pilot.pause()

        reverted_text = _label_text(label)
        assert "Enter send" in reverted_text, (
            f"after set_in_flight(False), hint should revert to normal; got: {reverted_text!r}"
        )
        assert "responding" not in reverted_text, (
            f"in-flight text leaked after unlock; got: {reverted_text!r}"
        )
        assert not bar.has_class("in-flight")


@pytest.mark.asyncio
async def test_set_in_flight_idempotent_does_not_corrupt_hint() -> None:
    """Tier 2: calling set_in_flight(True) twice leaves hint in blocked state.

    Idempotency is already tested in test_input_bar_double_submit_guard;
    this test specifically pins that the hint text is consistent (= not
    blanked) after a redundant call.
    """
    from textual.widgets import Label

    from reyn.interfaces.tui.app import ReynTUIApp
    from reyn.interfaces.tui.widgets import InputBar

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        bar = app.query_one("#inputbar", InputBar)
        label = app.query_one("#hints", Label)

        bar.set_in_flight(True)
        await pilot.pause()
        bar.set_in_flight(True)   # idempotent — no state change, no label flicker
        await pilot.pause()

        text = _label_text(label)
        assert "responding" in text, (
            f"redundant set_in_flight(True) corrupted hint; got: {text!r}"
        )


# ── Option A: held-Enter feedback (border flash + transient hint) ─────────────


async def _wait_until(pilot, predicate, *, timeout: float = 2.0) -> bool:
    """Poll the event loop until ``predicate()`` is true or timeout elapses.

    Held-flash clears via a real Textual ``set_timer`` (wall-clock), so a
    fixed ``pilot.pause(N)`` would be flaky. Poll instead
    (feedback_event_loop_dependent_test_polling).
    """
    import asyncio
    waited = 0.0
    step = 0.05
    while waited < timeout:
        if predicate():
            return True
        await pilot.pause(step)
        await asyncio.sleep(0)
        waited += step
    return predicate()


def _load_and_submit_while_inflight(app):
    """Type text, lock in-flight, then drive the real Enter→_submit path.

    Returns (bar, ta) for assertions. ``_submit`` is the handler the Enter
    keypress routes through; calling it directly keeps the test deterministic
    while still exercising the production code path.
    """
    from textual.widgets import TextArea

    from reyn.interfaces.tui.widgets import InputBar

    bar = app.query_one("#inputbar", InputBar)
    ta = app.query_one("#input", TextArea)
    ta.load_text("draft kept while busy")
    bar.set_in_flight(True)
    bar._submit(ta)
    return bar, ta


@pytest.mark.asyncio
async def test_held_enter_flashes_and_keeps_text() -> None:
    """Tier 2: Enter while in-flight flashes the border + swaps hint to held — and
    does NOT submit (text retained).

    Option A = pure feedback: the keypress is acknowledged (``.held-flash``
    class + ``_HINT_HELD`` footer) but the typed text stays in the TextArea
    for manual resubmit (= NOT cleared, NOT posted). Regression-guards the
    Wave-9 D-F11 double-submit protection at the same time.
    """
    from textual.widgets import Label

    from reyn.interfaces.tui.app import ReynTUIApp

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        bar, ta = _load_and_submit_while_inflight(app)
        await pilot.pause()
        label = app.query_one("#hints", Label)

        # Active feedback present.
        assert bar.has_class("held-flash"), "held-flash border class not applied"
        held_text = _label_text(label)
        assert "held" in held_text.lower(), (
            f"footer should acknowledge the held Enter; got: {held_text!r}"
        )
        # Pure feedback: text retained (not submitted/cleared).
        assert ta.text == "draft kept while busy", (
            f"typed text must survive an in-flight Enter; got: {ta.text!r}"
        )


@pytest.mark.asyncio
async def test_held_flash_clears_to_inflight_hint_when_still_busy() -> None:
    """Tier 2: after the flash timer the border clears and the footer restores
    to the in-flight hint (turn still running).
    """
    from textual.widgets import Label

    from reyn.interfaces.tui.app import ReynTUIApp

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        bar, _ta = _load_and_submit_while_inflight(app)
        await pilot.pause()
        assert bar.has_class("held-flash")

        cleared = await _wait_until(pilot, lambda: not bar.has_class("held-flash"))
        assert cleared, "held-flash never cleared after the flash timer"

        # Still in-flight → footer restores to the in-flight hint, not held.
        label = app.query_one("#hints", Label)
        restored = _label_text(label)
        assert "responding" in restored, (
            f"after flash, in-flight footer should return; got: {restored!r}"
        )


@pytest.mark.asyncio
async def test_held_flash_turn_end_midflash_restores_normal_no_stale() -> None:
    """Tier 2: a turn that ENDS during the held-flash restores the NORMAL hint
    (falsification — no stale held/in-flight text repainted).

    This is the stale-deferred-write guard
    (feedback_tui_deferred_timer_stale_removal_class): the restore must read
    the live in-flight state, and the in-flight transition must cancel the
    pending flash so a late timer can't repaint a stale footer.
    """
    from textual.widgets import Label

    from reyn.interfaces.tui.app import ReynTUIApp

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        bar, _ta = _load_and_submit_while_inflight(app)
        await pilot.pause()
        assert bar.has_class("held-flash")

        # Turn ends BEFORE the flash timer would fire.
        bar.set_in_flight(False)
        await pilot.pause()

        # Flash class dropped immediately; footer is the normal hint.
        assert not bar.has_class("held-flash"), (
            "in-flight transition must drop the held-flash class"
        )
        label = app.query_one("#hints", Label)
        text = _label_text(label)
        assert "Enter send" in text, f"normal hint should be restored; got: {text!r}"
        assert "held" not in text.lower(), f"stale held text leaked; got: {text!r}"
        assert "responding" not in text, f"stale in-flight text leaked; got: {text!r}"

        # And a late timer fire (if any) must not repaint stale text.
        await _wait_until(pilot, lambda: False, timeout=1.2)
        late = _label_text(label)
        assert "Enter send" in late and "held" not in late.lower(), (
            f"late flash timer repainted a stale footer; got: {late!r}"
        )
