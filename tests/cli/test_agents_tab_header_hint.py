"""Tier 2: agents tab header surfaces space=open + c=copy (H-F2).

Wave-10 Topic H finding F2 (P2): the agents tab header advertised
only ``j↓ k↑`` while the Memory tab next door advertised
``j↓ k↑ space=open c=copy``. Both tabs honor identical Space (open
preview) and ``c`` (copy bundle) keybindings — the agents tab
simply omitted the hint. First-time users on the agents tab had
to read the Keys tab to learn about Space / c.

After the fix the agents tab header mirrors Memory's hint shape.

Public surfaces tested:
  - the ``_panel_header_markup`` for ``"agents"`` includes
    ``space=open`` and ``c=copy``
  - the hint shape matches Memory's idiom (= readability uniform)
  - other tab headers are unchanged (regression guard)
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


@pytest.mark.asyncio
async def test_agents_header_surfaces_space_and_c_hints() -> None:
    """Tier 2: ``Agents`` header includes ``space=open`` and ``c=copy``."""
    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets import RightPanel

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        panel = app.query_one(RightPanel)
        panel._panel_type = "agents"
        markup = panel._panel_header_markup()
        assert "Agents" in markup
        assert "j↓ k↑" in markup
        assert "space=open" in markup, (
            f"agents header should surface space=open, got: {markup!r}"
        )
        assert "c=copy" in markup, (
            f"agents header should surface c=copy, got: {markup!r}"
        )


@pytest.mark.asyncio
async def test_agents_header_hint_matches_memory_idiom() -> None:
    """Tier 2: agents + memory headers carry the same set of action hints.

    Same keybindings → same advertised affordances. Pins the
    cross-tab consistency contract that H-F2 closes.
    """
    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets import RightPanel

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        panel = app.query_one(RightPanel)
        panel._panel_type = "memory"
        memory_hint_segment = panel._panel_header_markup().split("[#555555]")[-1]
        panel._panel_type = "agents"
        agents_hint_segment = panel._panel_header_markup().split("[#555555]")[-1]
        # Same actions advertised (= the trailing ``[/]`` markup chunk
        # is identical between the two headers).
        assert memory_hint_segment == agents_hint_segment, (
            f"agents and memory headers should advertise identical hints; "
            f"memory={memory_hint_segment!r}, agents={agents_hint_segment!r}"
        )


@pytest.mark.asyncio
async def test_other_tab_headers_unchanged() -> None:
    """Tier 2: keys / cost / docs / pending headers are not affected.

    Regression guard — H-F2 was scoped to the agents header only.
    """
    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets import RightPanel

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        panel = app.query_one(RightPanel)
        # Keys: minimal navigation hint
        panel._panel_type = "keys"
        assert "j↓ k↑" in panel._panel_header_markup()
        # Cost: minimal navigation hint
        panel._panel_type = "cost"
        assert "j↓ k↑" in panel._panel_header_markup()
        # Pending: discard + claim
        panel._panel_type = "pending"
        ph = panel._panel_header_markup()
        assert "d=discard" in ph and "c=claim" in ph
        # Docs: filter + open
        panel._panel_type = "docs"
        dh = panel._panel_header_markup()
        assert "space=open" in dh and "/=filter" in dh
