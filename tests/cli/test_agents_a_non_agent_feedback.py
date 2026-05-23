"""Tier 2: Agents tab ``a`` on a non-agent row walks up to owning agent.

Wave-11 finding A#6. Before this PR, pressing ``a`` while the
cursor sat on a running_skill / running_plan / recent_skill /
recent_plan row silently no-op'd. User got no feedback —
indistinguishable from "the key is broken". First-time-user
discoverability bug.

This PR: walk UP through ``_agents_items`` to the nearest
``kind == "agent"`` row and prefill /attach for THAT agent. The
agents tab orders items per-agent (header + that agent's sub-
rows), so the first agent above the cursor is the owning
agent. Net: ``a`` always does something meaningful regardless
of which sub-row the user landed on.

Edge case (= rare): no agent above the cursor → surface a
``_flash_status`` hint.

Pinned:
  - cursor on running_skill row → walks up to parent agent
  - cursor on recent_plan row → walks up to parent agent
  - cursor on agent row → unchanged from prior behaviour
  - empty agent list above cursor → flash hint, no prefill
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


@pytest.mark.asyncio
async def test_cursor_on_running_skill_walks_to_owning_agent() -> None:
    """Tier 2: ``a`` on a running_skill row prefills the parent agent's /attach."""
    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets import InputBar, RightPanel

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        panel = app.query_one("#right_panel", RightPanel)
        # Seed flat_items: alice agent + alice's running skill, then
        # bob agent + bob's running skill. Cursor lands on alice's
        # skill row.
        panel._agents_items = [
            {"kind": "agent", "name": "alice"},
            {"kind": "running_skill", "skill_name": "code_review"},
            {"kind": "agent", "name": "bob"},
            {"kind": "running_skill", "skill_name": "shell"},
        ]
        panel._agents_cursor = 1  # alice's running skill
        panel._prefill_attach_for_cursor()
        await pilot.pause()
        ib = app.query_one("#inputbar", InputBar)
        ta = ib.query_one("#input")
        assert ta.text == "/attach alice"


@pytest.mark.asyncio
async def test_cursor_on_bobs_sub_row_walks_to_bob_not_alice() -> None:
    """Tier 2: walk-up stops at the NEAREST agent header above cursor."""
    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets import InputBar, RightPanel

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        panel = app.query_one("#right_panel", RightPanel)
        panel._agents_items = [
            {"kind": "agent", "name": "alice"},
            {"kind": "running_skill", "skill_name": "code_review"},
            {"kind": "agent", "name": "bob"},
            {"kind": "recent_plan", "plan_id": "p1"},
        ]
        panel._agents_cursor = 3  # bob's recent plan
        panel._prefill_attach_for_cursor()
        await pilot.pause()
        ib = app.query_one("#inputbar", InputBar)
        ta = ib.query_one("#input")
        assert ta.text == "/attach bob"


@pytest.mark.asyncio
async def test_cursor_on_agent_row_unchanged_behaviour() -> None:
    """Tier 2: cursor directly on agent header → original prefill path."""
    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets import InputBar, RightPanel

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        panel = app.query_one("#right_panel", RightPanel)
        panel._agents_items = [
            {"kind": "agent", "name": "charlie"},
        ]
        panel._agents_cursor = 0
        panel._prefill_attach_for_cursor()
        await pilot.pause()
        ib = app.query_one("#inputbar", InputBar)
        ta = ib.query_one("#input")
        assert ta.text == "/attach charlie"


@pytest.mark.asyncio
async def test_no_agent_above_cursor_flashes_hint() -> None:
    """Tier 2: when no agent header exists above a non-agent cursor → flash."""
    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets import InputBar, RightPanel

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        panel = app.query_one("#right_panel", RightPanel)
        # Pathological state: a sub-row without any preceding agent
        # header. In practice render_agents shouldn't emit this, but
        # the helper guards against it.
        panel._agents_items = [
            {"kind": "running_skill", "skill_name": "orphan"},
        ]
        panel._agents_cursor = 0
        panel._prefill_attach_for_cursor()
        await pilot.pause()
        # InputBar text should NOT have been prefilled.
        ib = app.query_one("#inputbar", InputBar)
        ta = ib.query_one("#input")
        assert "/attach" not in ta.text


@pytest.mark.asyncio
async def test_empty_agents_items_silent_no_op() -> None:
    """Tier 2: empty list → silent no-op (existing behaviour preserved)."""
    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets import RightPanel

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        panel = app.query_one("#right_panel", RightPanel)
        panel._agents_items = []
        panel._agents_cursor = 0
        # Should not raise.
        panel._prefill_attach_for_cursor()
        await pilot.pause()
