"""Tier 2: agents tab `a` key prefills /attach for the cursor's agent (H-F11).

Wave-10 follow-up Topic H finding F11 (P2): the agents tab had
no keyboard shortcut to switch the attached agent. Users had to
type ``/agent switch <name>`` (or ``/attach <name>``) by hand
from the input bar, even though the cursor was already on the
target agent's row.

After the fix ``a`` on the agents tab prefills ``/attach <name>``
into the InputBar (= same handoff pattern as the docs tab ``/``
→ ``/docs-filter`` prefill). MVP — confirmation still requires
Enter so the user can edit / abort; the slash command performs
the actual switch.

Public surfaces tested:
  - ``a`` on agent-label cursor → InputBar contains
    ``/attach <name>``
  - ``a`` on non-agent cursor (= running skill / recent plan
    row) is silently ignored (= action only meaningful on agent-
    label rows)
  - Keys tab surfaces the new key in PANEL group
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


@pytest.mark.asyncio
async def test_a_key_on_agent_row_prefills_attach() -> None:
    """Tier 2: ``a`` on agent-label cursor → ``/attach <name>`` in InputBar."""
    from textual.widgets import TextArea

    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets import RightPanel

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        panel = app.query_one(RightPanel)
        panel._panel_type = "agents"
        panel._agents_items = [
            {
                "kind": "agent",
                "name": "target-agent",
                "attached": False,
                "loaded": True,
            },
        ]
        panel._agents_cursor = 0

        panel._prefill_attach_for_cursor()
        await pilot.pause()

        ta = app.query_one("#input", TextArea)
        assert ta.text == "/attach target-agent", (
            f"InputBar should prefill /attach target-agent, got {ta.text!r}"
        )


@pytest.mark.asyncio
async def test_a_key_on_non_agent_row_is_silent_noop() -> None:
    """Tier 2: ``a`` on running_skill / recent_plan cursor does nothing.

    The action is meaningful only on the agent-label row; lower
    rows in the tree (= running skills, recent plans) should not
    dispatch a meaningless ``/attach <skill_name>``.
    """
    from textual.widgets import TextArea

    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets import RightPanel

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        panel = app.query_one(RightPanel)
        panel._panel_type = "agents"
        panel._agents_items = [
            {"kind": "running_skill", "skill_name": "test_skill", "run_id": "rid"},
        ]
        panel._agents_cursor = 0

        ta = app.query_one("#input", TextArea)
        pre_text = ta.text

        panel._prefill_attach_for_cursor()
        await pilot.pause()

        assert ta.text == pre_text, (
            f"non-agent cursor should leave InputBar unchanged; "
            f"pre={pre_text!r} post={ta.text!r}"
        )


@pytest.mark.asyncio
async def test_a_key_on_empty_agents_tab_is_safe() -> None:
    """Tier 2 (regression): empty flat_items → safe no-op (no IndexError)."""
    from textual.widgets import TextArea

    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets import RightPanel

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        panel = app.query_one(RightPanel)
        panel._panel_type = "agents"
        panel._agents_items = []
        panel._agents_cursor = 0
        # Must not raise.
        panel._prefill_attach_for_cursor()
        await pilot.pause()
        ta = app.query_one("#input", TextArea)
        assert ta.text == ""


def test_keys_tab_surfaces_a_attach_in_panel_group() -> None:
    """Tier 2: Keys tab markup lists the new ``a`` action.

    Discoverability: a user reading the Keys tab should be able to
    learn about the ``a`` action without source-diving.
    """
    import asyncio

    from reyn.chat.tui.app import ReynTUIApp

    async def _grab_markup() -> str:
        from reyn.chat.tui.widgets.right_panel.keys_tab import render_keys
        app = ReynTUIApp(
            registry=None, agent_name="t", model="m", budget_tracker=None,
        )
        async with app.run_test(headless=True) as pilot:
            await pilot.pause()
            markup, _ = render_keys(app)
            return markup

    markup = asyncio.run(_grab_markup())
    assert "a" in markup
    assert "Attach to cursor agent" in markup, (
        f"Keys tab should describe the new attach action; got:\n{markup!r}"
    )
