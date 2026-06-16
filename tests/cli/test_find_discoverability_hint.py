"""Tier 2: /find surfaces the Ctrl+G cycle keybinding for discoverability.

Follow-on to PR #537 (/find MVP) + PR #539 (Ctrl+G match cycle).
The cycle keybinding is invisible without either reading the
Keys tab or the source — users who ran ``/find`` would never
discover that Ctrl+G steps through subsequent matches. This pins:

  1. When the initial /find lands ≥ 2 matches, the status line
     trailing-appends ``"· Ctrl+G next"`` so the keybinding
     surfaces on the exact UX moment users care about it.
  2. The 1-match case suppresses the hint (= nothing to cycle to).
  3. The Keys tab groups ``ctrl+g`` and ``ctrl+shift+g`` under
     CONVERSATION (= their semantic home alongside Ctrl+P/N turn
     jump), not the generic GLOBAL fallback.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


@pytest.mark.asyncio
async def test_initial_find_status_includes_ctrl_g_hint_when_multi_match() -> None:
    """Tier 2: status string includes ``Ctrl+G next`` when matches ≥ 2."""
    from rich.text import Text

    from reyn.chat.outbox import OutboxMessage
    from reyn.interfaces.tui.app import ReynTUIApp
    from reyn.interfaces.tui.app_outbox import OutboxRouter
    from reyn.interfaces.tui.widgets import ConversationView

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        header = app.query_one("#header")
        log = conv._log()
        log.write(Text("alpha mark"))
        log.write(Text("beta mark"))
        log.write(Text("gamma mark"))
        await pilot.pause()
        router = OutboxRouter(app)
        router._on_find(
            OutboxMessage(kind="__find__", text="mark"),
            conv,
            header,
        )
        await pilot.pause()
        snap = conv._sticky().snapshot()  # type: ignore[union-attr]
        assert snap["active"] is True
        body = snap["body"]
        assert "3 matches for 'mark'" in body
        assert "Ctrl+G next" in body


@pytest.mark.asyncio
async def test_initial_find_status_omits_hint_when_single_match() -> None:
    """Tier 2: single-match status does NOT include the cycle hint.

    With only one match, Ctrl+G would just cycle back to itself
    (= len-1 wrap). Surfacing the hint would imply there's
    "next" to go to. Suppress when exactly one match is present.
    """
    from rich.text import Text

    from reyn.chat.outbox import OutboxMessage
    from reyn.interfaces.tui.app import ReynTUIApp
    from reyn.interfaces.tui.app_outbox import OutboxRouter
    from reyn.interfaces.tui.widgets import ConversationView

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        header = app.query_one("#header")
        log = conv._log()
        log.write(Text("alpha lone-needle here"))
        log.write(Text("beta unrelated"))
        await pilot.pause()
        router = OutboxRouter(app)
        router._on_find(
            OutboxMessage(kind="__find__", text="lone-needle"),
            conv,
            header,
        )
        await pilot.pause()
        snap = conv._sticky().snapshot()  # type: ignore[union-attr]
        assert "1 match for 'lone-needle'" in snap["body"]
        assert "Ctrl+G" not in snap["body"]


def test_keys_tab_groups_find_cycle_under_conversation() -> None:
    """Tier 2: ctrl+g / ctrl+shift+g land in CONVERSATION group, not GLOBAL.

    Pins the semantic home of the cycle keys. Falling back into
    GLOBAL would scatter them away from the other conv-pane
    navigation keys (Ctrl+P / Ctrl+N turn jump) which is exactly
    where users will look first.
    """
    from reyn.interfaces.tui.widgets.right_panel.keys_tab import _key_group_for

    assert _key_group_for("ctrl+g") == "CONVERSATION"
    assert _key_group_for("ctrl+shift+g") == "CONVERSATION"


@pytest.mark.asyncio
async def test_keys_tab_render_includes_find_cycle_descriptions() -> None:
    """Tier 2: rendered Keys tab markup includes the cycle keys with descriptions.

    End-to-end check that the binding descriptions (= "Find next
    match" / "Find prev match") flow through ``render_keys`` to
    the visible markup so users actually see them in the panel.
    """
    from reyn.interfaces.tui.app import ReynTUIApp
    from reyn.interfaces.tui.widgets.right_panel.keys_tab import render_keys

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        markup, _, _ = render_keys(app)
        assert "Find next match" in markup
        assert "Find prev match" in markup
        # Pretty-printed form of ctrl+g / ctrl+shift+g.
        assert "⌃G" in markup
