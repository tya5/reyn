"""Tier 2: agents-tab j/k cursor lands on agent-name rows.

Previously ``flat_items`` only included running-skill / plan / recent
rows — agent-name rows were rendered but not selectable, so j/k did
nothing when no skills were in flight. Pin the new contract: every
agent gets a ``{"kind": "agent"}`` entry in flat_items keyed at the
label's y position.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


@pytest.mark.asyncio
async def test_agents_tab_includes_agent_rows_in_flat_items(tmp_path):
    """Tier 2: every agent gets a selectable row in ``flat_items``.

    Drives ``render_agents`` through a real ``AgentRegistry`` on
    tmp_path with two agents created and no skills running — the
    failure mode the sub-agent observed was "j/k produces no cursor
    movement at all" because the only flat_items were skill rows and
    there were zero skills.
    """
    from reyn.chat.registry import AgentRegistry
    from reyn.chat.tui.widgets.right_panel.agents_tab import render_agents

    def _factory(profile):
        return object()

    registry = AgentRegistry(
        project_root=tmp_path,
        session_factory=_factory,
        state_log=None,
    )
    registry.create("alpha")
    registry.create("beta")

    _, flat_items, item_ys = render_agents(
        registry, exec_state={}, cursor=0,
    )

    agent_items = [it for it in flat_items if it.get("kind") == "agent"]
    # ``default`` is auto-created by AgentRegistry — match what the
    # production registry actually returns rather than just the two we
    # asked for.
    names = {it["name"] for it in agent_items}
    assert {"alpha", "beta"}.issubset(names), (
        f"both newly-created agents must appear in flat_items; got {names!r}"
    )
    # item_ys must match flat_items length 1:1 (= the navigation
    # contract the parent panel relies on).
    assert len(item_ys) == len(flat_items)


@pytest.mark.asyncio
async def test_agents_tab_agent_item_carries_attached_loaded_flags(tmp_path):
    """Tier 2: ``{kind:agent}`` items carry the ``attached``/``loaded`` flags
    so a future ``Space``/``Enter`` action can route by current state."""
    from reyn.chat.registry import AgentRegistry
    from reyn.chat.tui.widgets.right_panel.agents_tab import render_agents

    def _factory(profile):
        return object()

    registry = AgentRegistry(
        project_root=tmp_path,
        session_factory=_factory,
        state_log=None,
    )
    registry.create("alpha")
    registry.create("beta")

    _, flat_items, _ = render_agents(
        registry, exec_state={}, cursor=0,
    )
    agent_items = [it for it in flat_items if it.get("kind") == "agent"]
    for it in agent_items:
        assert "attached" in it
        assert "loaded" in it
        assert isinstance(it["attached"], bool)
        assert isinstance(it["loaded"], bool)
