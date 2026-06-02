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

    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets import InputBar

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

    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets import InputBar

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

    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets import InputBar

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
