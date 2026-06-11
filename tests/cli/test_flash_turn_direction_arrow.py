"""Tier 2: _flash_turn_position arrow reflects navigation direction (G-F5).

Wave-10 Topic G finding F5 (P2): ``_flash_turn_position`` hard-coded
``↑ turn N / M`` regardless of whether the user pressed Ctrl+P
(backward → ``↑``) or Ctrl+N (forward → ``↓``). Forward navigation
showed an upward arrow, contradicting the actual movement and
misleading users who use the arrow as a navigation cue.

Fix: ``_flash_turn_position`` takes a ``delta`` parameter; negative
delta → ``↑``, positive delta → ``↓``. ``_jump_to_relative_anchor``
passes its own ``delta`` through.

Public surfaces tested:
  - backward jump (delta=-1) → sticky body starts with ``↑``
  - forward jump (delta=+1) → sticky body starts with ``↓``
  - the rest of the body shape ``turn N / M`` is unchanged
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


@pytest.mark.asyncio
async def test_backward_jump_renders_up_arrow() -> None:
    """Tier 2: Ctrl+P (delta=-1) → ``↑ turn N / M`` in sticky."""
    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets import ConversationView

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        log = conv._log()
        conv._scroll_ctrl._turn_anchors = [5, 50, 100]
        try:
            log.scroll_to(y=100, animate=False)
        except Exception:
            pass
        await pilot.pause()

        conv._scroll_ctrl._jump_to_relative_anchor(-1)
        await pilot.pause()
        snap = conv._sticky().snapshot()
        assert snap["active"]
        assert snap["body"].startswith("↑ turn"), (
            f"backward jump should render ↑, got body={snap['body']!r}"
        )
        # Body shape: ↑ turn N / M
        assert " turn " in snap["body"]
        assert " / " in snap["body"]


@pytest.mark.asyncio
async def test_forward_jump_renders_down_arrow() -> None:
    """Tier 2: Ctrl+N (delta=+1) → ``↓ turn N / M`` in sticky.

    Pre-fix this was ``↑ turn N / M`` — wrong direction.
    """
    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets import ConversationView

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        log = conv._log()
        conv._scroll_ctrl._turn_anchors = [5, 50, 100]
        try:
            log.scroll_to(y=10, animate=False)
        except Exception:
            pass
        await pilot.pause()

        conv._scroll_ctrl._jump_to_relative_anchor(+1)
        await pilot.pause()
        snap = conv._sticky().snapshot()
        assert snap["active"]
        assert snap["body"].startswith("↓ turn"), (
            f"forward jump should render ↓, got body={snap['body']!r}"
        )


def test_flash_turn_position_signature_accepts_delta_kwarg() -> None:
    """Tier 2: ``delta`` kwarg defaults to -1 (backward) for backward compat.

    Older callers that pass only ``(n, total)`` should continue to get
    the legacy ``↑`` arrow.
    """
    import inspect

    from reyn.chat.tui.widgets.conversation import _ScrollController
    sig = inspect.signature(_ScrollController._flash_turn_position)
    assert "delta" in sig.parameters
    # Default is backward (preserves legacy behavior for any caller
    # that doesn't pass the new arg).
    assert sig.parameters["delta"].default == -1
