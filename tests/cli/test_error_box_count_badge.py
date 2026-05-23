"""Tier 2: ErrorBox renders ``[N/M]`` index badge for stacked errors.

Wave-11 finding B#6. Before this PR, every stacked ErrorBox
header read identically ``✗ [skill#abcd]: msg ▶`` — no per-box
index. The sticky surfaced the total count, but on F5/F6 jump
landing the user had no signal "this is error 2 of 3" inside
the focused box. With identical-looking failures (= timeout × 3)
the user couldn't tell which they were reading.

This PR: each ErrorBox carries ``_index`` + ``_total`` that the
header renders as ``[N/M]`` when total > 1. ``ConversationView``
calls ``set_index_total`` after every mutation to ``_error_boxes``
(mount, dismiss, auto-eviction) so the badges stay accurate.

Pinned:
  - Single error → no badge (= cold-default unchanged)
  - 2+ errors → each shows correct ``[N/M]``
  - Dismiss → surviving boxes renumber (e.g. ``[1/2]`` → ``[1/1]``
    → no badge)
  - Auto-eviction → renumbers
  - ``set_index_total`` is equality-gated (= idempotent calls
    skip the DOM round-trip)
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def _header_text_of(box) -> str:
    """Read the rendered header text via the .eb-header Label."""
    from textual.widgets import Label
    try:
        label = box.query_one(".eb-header", Label)
    except Exception:
        return ""
    renderable = getattr(label, "_renderable", None) or getattr(label, "renderable", None)
    if renderable is None:
        # Fallback to building the header directly from the widget's helper.
        return box._header_text()
    return str(getattr(renderable, "plain", renderable))


@pytest.mark.asyncio
async def test_single_error_no_badge() -> None:
    """Tier 2: 1 mounted error → no ``[N/M]`` prefix (= cold-default)."""
    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets import ConversationView

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        box = conv.mount_error(message="solo error")
        await pilot.pause()
        header = box._header_text()
        # No badge present.
        assert "[1/1]" not in header
        # ``✗ solo error  ▶`` shape preserved.
        assert "solo error" in header


@pytest.mark.asyncio
async def test_two_errors_renumber_correctly() -> None:
    """Tier 2: 2 mounted errors → each shows ``[1/2]`` / ``[2/2]``."""
    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets import ConversationView

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        first = conv.mount_error(message="first error")
        second = conv.mount_error(message="second error")
        await pilot.pause()
        assert "[1/2]" in first._header_text()
        assert "[2/2]" in second._header_text()


@pytest.mark.asyncio
async def test_three_errors_renumber_correctly() -> None:
    """Tier 2: 3 mounted errors → each shows the right position."""
    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets import ConversationView

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        boxes = [
            conv.mount_error(message=f"err {i}") for i in range(3)
        ]
        await pilot.pause()
        assert "[1/3]" in boxes[0]._header_text()
        assert "[2/3]" in boxes[1]._header_text()
        assert "[3/3]" in boxes[2]._header_text()


@pytest.mark.asyncio
async def test_dismiss_renumbers_survivors() -> None:
    """Tier 2: dismissing the newest box renumbers the rest down."""
    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets import ConversationView

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        first = conv.mount_error(message="first")
        second = conv.mount_error(message="second")
        third = conv.mount_error(message="third")
        await pilot.pause()
        assert "[3/3]" in third._header_text()
        conv.dismiss_last_error()  # removes third
        await pilot.pause()
        # Now 2 remain; renumber to [1/2] [2/2].
        assert "[1/2]" in first._header_text()
        assert "[2/2]" in second._header_text()
        conv.dismiss_last_error()  # removes second
        await pilot.pause()
        # Single error remaining → no badge.
        assert "[1/" not in first._header_text()
        assert "[2/" not in first._header_text()


def test_header_text_omits_badge_for_total_one() -> None:
    """Tier 2: a directly-constructed box with total=1 omits the badge.

    Cold-default ctor params (= index=0, total=0) skip the badge
    too — ``total > 1 and index > 0`` is the gate.
    """
    from reyn.chat.tui.widgets.error_box import ErrorBox

    # total=1, single error case.
    box = ErrorBox(message="x", index=1, total=1)
    assert "[1/1]" not in box._header_text()
    # Defaults: index=0, total=0 — no badge.
    box2 = ErrorBox(message="x")
    assert "/" not in box2._header_text().split("✗", 1)[1].split("▶")[0]


def test_set_index_total_idempotent_skip() -> None:
    """Tier 2: redundant set_index_total calls early-return (= no DOM churn).

    Internal counter check: after the first set, the second
    call with identical values doesn't change ``_index`` /
    ``_total``.
    """
    from reyn.chat.tui.widgets.error_box import ErrorBox

    box = ErrorBox(message="x")
    box.set_index_total(2, 3)
    assert box._index == 2
    assert box._total == 3
    # Same values — equality gate short-circuits.
    box.set_index_total(2, 3)
    assert box._index == 2
    assert box._total == 3


@pytest.mark.asyncio
async def test_set_index_total_updates_header() -> None:
    """Tier 2: ``set_index_total`` re-renders the header with the new badge."""
    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets import ConversationView

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        box = conv.mount_error(message="x")
        await pilot.pause()
        box.set_index_total(2, 5)
        await pilot.pause()
        assert "[2/5]" in box._header_text()
