"""Tier 2: render_agents nests sub-skill rows under their parent.

Issue #210 — the OS-side ``parent_run_id`` stamp (PR #198) already lands
on every sub-skill trace's ``OutboxMessage.meta``; the TUI consumes it
when building the right-panel agents tree.

Contract pinned here (public surface of ``render_agents``):

1. ``flat_items`` carries ``parent_run_id`` for every running_skill entry.
2. A skill with ``parent_run_id`` matching another running skill's
   ``run_id`` (on the same agent) is rendered as a *child* of that
   parent — verified via flat_items ordering (parent emits first) and
   the absence of duplicate roots.
3. Children whose parent is NOT among the running skills (= parent
   already finished, or on a different agent) gracefully fall back to
   root rendering — no crash, no orphan.

Cursor / item_ys logic is exercised but specific y-coordinates are not
pinned (algorithm-level layout per testing.ja.md).
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def _make_registry(tmp_path):
    from reyn.chat.registry import AgentRegistry

    def _factory(profile):
        return object()

    return AgentRegistry(
        project_root=tmp_path, session_factory=_factory, state_log=None,
    )


def _exec_entry(skill_name: str, parent_run_id: str = ""):
    return {
        "skill_name": skill_name,
        "agent_name": "default",
        "start_time": time.monotonic(),
        "phase": "phase_a",
        "phase_visits": 1,
        "triggered_by": "test",
        "parent_run_id": parent_run_id,
    }


@pytest.mark.asyncio
async def test_running_skill_items_carry_parent_run_id_field(tmp_path):
    """Tier 2: ``running_skill`` flat_items expose ``parent_run_id``."""
    from reyn.chat.tui.widgets.right_panel.agents_tab import render_agents

    registry = _make_registry(tmp_path)
    exec_state = {"r-A": _exec_entry("root_skill", parent_run_id="")}

    _, flat_items, _ = render_agents(registry, exec_state, cursor=0)
    skill_items = [i for i in flat_items if i.get("kind") == "running_skill"]
    assert len(skill_items) == 1
    assert "parent_run_id" in skill_items[0]
    assert skill_items[0]["parent_run_id"] == ""


@pytest.mark.asyncio
async def test_parent_emits_before_child_in_flat_items(tmp_path):
    """Tier 2: a child references its parent and the parent emits first.

    Pins the topological-order contract: roots first, then children.
    Without it, cursor navigation could land on a child before its
    parent is even visible in the flat list.
    """
    from reyn.chat.tui.widgets.right_panel.agents_tab import render_agents

    registry = _make_registry(tmp_path)
    # Insert child BEFORE parent in the dict to verify ordering is not
    # accidental (dict iteration order would put child first).
    exec_state = {
        "r-B": _exec_entry("child_skill", parent_run_id="r-A"),
        "r-A": _exec_entry("root_skill"),
    }

    _, flat_items, _ = render_agents(registry, exec_state, cursor=0)
    skill_items = [i for i in flat_items if i.get("kind") == "running_skill"]
    run_ids_in_order = [i["run_id"] for i in skill_items]
    assert run_ids_in_order.index("r-A") < run_ids_in_order.index("r-B"), (
        f"parent r-A must emit before child r-B; got {run_ids_in_order!r}"
    )


@pytest.mark.asyncio
async def test_orphan_child_falls_back_to_root_render(tmp_path):
    """Tier 2: a child whose ``parent_run_id`` is not in exec_state still renders.

    Edge case: the parent has already finished (rotated out of
    ``_skill_exec``) but the child trace still carries the parent id.
    The render must not crash and must still surface the child somewhere
    — falling back to root-row rendering is the contract.
    """
    from reyn.chat.tui.widgets.right_panel.agents_tab import render_agents

    registry = _make_registry(tmp_path)
    exec_state = {
        "r-B": _exec_entry("orphan_child", parent_run_id="r-A-finished"),
    }

    _, flat_items, _ = render_agents(registry, exec_state, cursor=0)
    skill_items = [i for i in flat_items if i.get("kind") == "running_skill"]
    assert len(skill_items) == 1
    assert skill_items[0]["run_id"] == "r-B"


@pytest.mark.asyncio
async def test_two_children_one_parent_all_emit(tmp_path):
    """Tier 2: multiple children of the same parent all appear in flat_items.

    Pins that the topological pass doesn't accidentally drop siblings
    (= a "second child overwrites first" bug would corrupt navigation).
    """
    from reyn.chat.tui.widgets.right_panel.agents_tab import render_agents

    registry = _make_registry(tmp_path)
    exec_state = {
        "r-A": _exec_entry("root_skill"),
        "r-B1": _exec_entry("child_one", parent_run_id="r-A"),
        "r-B2": _exec_entry("child_two", parent_run_id="r-A"),
    }

    _, flat_items, _ = render_agents(registry, exec_state, cursor=0)
    skill_items = [i for i in flat_items if i.get("kind") == "running_skill"]
    run_ids = {i["run_id"] for i in skill_items}
    assert run_ids == {"r-A", "r-B1", "r-B2"}
    # Item count must equal flat_items length for the parent + 2 children.
    # (Other agents may add their own rows but with one agent and three
    # skills, we expect exactly 3 running_skill entries.)
    assert len(skill_items) == 3
