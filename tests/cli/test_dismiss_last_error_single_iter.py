"""Tier 2: dismiss_last_error always writes breadcrumb for most-recent box (I-F3).

Wave-10 follow-up Topic I finding F3 (P2): the previous
``while self._error_boxes: ... continue`` loop swallowed the
breadcrumb for the actual most-recently-mounted box if its
``remove()`` raised, then fell through to write a breadcrumb for
the NEXT-most-recent box. The docstring promised "the most
recently mounted" singular; the implementation was "the most
recently mounted that can be removed without raising".

After the fix the method:
  - pops a single box (= the most recent, ``[-1]``)
  - writes the breadcrumb FIRST (= load-bearing per F2 intent)
  - best-effort ``remove()`` — failure does not affect the
    breadcrumb path

Public surfaces tested:
  - normal dismiss → breadcrumb for the most recent + remove
  - dismiss with no boxes → no-op
  - sequential dismisses pop in last-in-first-out order
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def _log_text(conv) -> str:
    log = conv._log()
    return "\n".join(getattr(s, "text", "") for s in getattr(log, "lines", []))


@pytest.mark.asyncio
async def test_dismiss_writes_breadcrumb_for_most_recent_error() -> None:
    """Tier 2: breadcrumb names the most recently mounted box."""
    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets import ConversationView

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        conv.mount_error(message="oldest error message")
        conv.mount_error(message="middle error message")
        conv.mount_error(message="most recent error message")
        await pilot.pause()
        assert len(conv._error_boxes) == 3

        conv.dismiss_last_error()
        await pilot.pause()

        # Most recent box was dismissed → breadcrumb names IT.
        log_text = _log_text(conv)
        assert "most recent error message" in log_text, (
            f"breadcrumb should reference the most recent box; "
            f"log:\n{log_text!r}"
        )
        # The other two still in the list.
        assert len(conv._error_boxes) == 2


@pytest.mark.asyncio
async def test_dismiss_with_no_errors_is_noop() -> None:
    """Tier 2 (regression): empty list → safe no-op."""
    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets import ConversationView

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        # Must not raise.
        conv.dismiss_last_error()
        await pilot.pause()
        assert conv._error_boxes == []


@pytest.mark.asyncio
async def test_sequential_dismisses_pop_last_in_first_out() -> None:
    """Tier 2: three sequential dismisses → most-recent removed first."""
    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets import ConversationView

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        conv.mount_error(message="A oldest")
        conv.mount_error(message="B middle")
        conv.mount_error(message="C newest")
        await pilot.pause()

        conv.dismiss_last_error()  # → C
        conv.dismiss_last_error()  # → B
        conv.dismiss_last_error()  # → A
        await pilot.pause()

        assert conv._error_boxes == []
        log_text = _log_text(conv)
        # All three breadcrumbs written, in last-in-first-out order.
        idx_C = log_text.find("C newest")
        idx_B = log_text.find("B middle")
        idx_A = log_text.find("A oldest")
        assert idx_C != -1 and idx_B != -1 and idx_A != -1, (
            f"all three dismissed errors should leave breadcrumbs; "
            f"log:\n{log_text!r}"
        )
        # Order in log: C breadcrumb first (= most recent), then B, then A.
        assert idx_C < idx_B < idx_A, (
            f"breadcrumbs should appear in dismissal order (LIFO); "
            f"idx_C={idx_C} idx_B={idx_B} idx_A={idx_A}"
        )
