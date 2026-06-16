"""Tier 2: agents tab renders ``[plan N/M]`` badge for plan-step skill rows.

issue #427 L4 step 6 — agents tab refactor for phase / plan / nest
depth. This PR addresses the wave-7 Topic C-F2 finding (= "agents-tab
running-skill tree lacks plan step attribution") by surfacing the
plan-step badge alongside the running skill row, matching the conv
pane SkillActivityRow's persistent plan badge (wave-7 PR #418).

Contract pinned here:

1. ``running_skill`` flat_items carry ``plan_n_done`` / ``plan_n_total``
   so cursor / preview / future actions can route by plan attribution.
2. When both fields are present + > 0, the rendered tree label
   includes a ``[plan N/M]`` badge after the skill name.
3. When the fields are absent / 0, no badge appears (= cold-default
   layout unchanged for non-planner skills).
4. Badge survives nested sub-skill rendering (= ``parent_run_id``
   nesting from issue #210 + the plan badge are independent axes).

The plan_n_done / plan_n_total values are populated in ``_skill_exec``
by ``ReynTUIApp._update_skill_exec`` when it parses
``ChatEventForwarder``'s ``"detail: plan N/M"`` trace text. This test
exercises the consumer side (= render_agents) by passing a fake
exec_state — the parser side is a small string-pattern op covered by
inspection.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def _render_to_plain(renderable) -> str:
    """Capture a Rich renderable's plain-text output via an isolated console.

    ``render_agents`` returns a ``rich.console.Group`` (header + tree);
    asserting on substring presence requires rendering through a real
    console rather than relying on a ``.plain`` attribute that only
    exists on ``Text``.
    """
    from io import StringIO

    from rich.console import Console
    buf = StringIO()
    console = Console(file=buf, force_terminal=False, width=200)
    console.print(renderable)
    return buf.getvalue()


def _make_registry(tmp_path):
    from reyn.chat.registry import AgentRegistry

    def _factory(profile):
        return object()

    return AgentRegistry(
        project_root=tmp_path, session_factory=_factory, state_log=None,
    )


def _exec_entry(
    skill_name: str,
    *,
    plan_n_done: int | None = None,
    plan_n_total: int | None = None,
    parent_run_id: str = "",
):
    entry = {
        "skill_name": skill_name,
        "agent_name": "default",
        "start_time": time.monotonic(),
        "phase": "resolve",
        "phase_visits": 1,
        "triggered_by": "test",
        "parent_run_id": parent_run_id,
    }
    if plan_n_done is not None:
        entry["plan_n_done"] = plan_n_done
    if plan_n_total is not None:
        entry["plan_n_total"] = plan_n_total
    return entry


@pytest.mark.asyncio
async def test_running_skill_with_plan_step_carries_fields_in_flat_items(tmp_path):
    """Tier 2b: plan_n_done / plan_n_total surface in flat_items."""
    from reyn.tui.widgets.right_panel.agents_tab import render_agents

    registry = _make_registry(tmp_path)
    exec_state = {
        "r-1": _exec_entry("planner_sub", plan_n_done=2, plan_n_total=5),
    }
    _, flat_items, _ = render_agents(registry, exec_state, cursor=0)
    skill_items = [i for i in flat_items if i.get("kind") == "running_skill"]
    assert skill_items, "expected at least one running_skill item in flat_items"
    assert skill_items[0]["plan_n_done"] == 2
    assert skill_items[0]["plan_n_total"] == 5


@pytest.mark.asyncio
async def test_running_skill_without_plan_step_omits_badge_in_render(tmp_path):
    """Tier 2b: skills without plan_n_done/plan_n_total render no badge."""
    from reyn.tui.widgets.right_panel.agents_tab import render_agents

    registry = _make_registry(tmp_path)
    exec_state = {"r-1": _exec_entry("non_planner_skill")}
    rendered, flat_items, _ = render_agents(
        registry, exec_state, cursor=0,
    )
    skill_items = [i for i in flat_items if i.get("kind") == "running_skill"]
    assert skill_items, "expected at least one running_skill item in flat_items"
    # The badge text "[plan " must NOT appear in the rendered tree output.
    plain = _render_to_plain(rendered)
    assert "[plan " not in plain
    # flat_items still carries the keys (= None) for schema consistency.
    assert skill_items[0].get("plan_n_done") is None
    assert skill_items[0].get("plan_n_total") is None


@pytest.mark.asyncio
async def test_running_skill_with_plan_step_renders_badge_text(tmp_path):
    """Tier 2b: ``[plan N/M]`` substring appears in the rendered output."""
    from reyn.tui.widgets.right_panel.agents_tab import render_agents

    registry = _make_registry(tmp_path)
    exec_state = {
        "r-1": _exec_entry("planner_sub", plan_n_done=3, plan_n_total=7),
    }
    rendered, _, _ = render_agents(registry, exec_state, cursor=0)
    plain = _render_to_plain(rendered)
    assert "[plan 3/7]" in plain


@pytest.mark.asyncio
async def test_plan_badge_survives_subskill_nesting(tmp_path):
    """Tier 2b: nested sub-skill with plan_step still renders the badge.

    Verifies the badge axis is orthogonal to the parent_run_id nesting
    axis — both can coexist on the same row.
    """
    from reyn.tui.widgets.right_panel.agents_tab import render_agents

    registry = _make_registry(tmp_path)
    exec_state = {
        "r-root": _exec_entry("planner"),
        "r-child": _exec_entry(
            "planner_sub", plan_n_done=2, plan_n_total=4,
            parent_run_id="r-root",
        ),
    }
    rendered, flat_items, _ = render_agents(
        registry, exec_state, cursor=0,
    )
    plain = _render_to_plain(rendered)
    # Both rows present, child carries badge.
    assert "planner" in plain
    assert "planner_sub" in plain
    assert "[plan 2/4]" in plain
    # Topology preserved: parent emits before child.
    skill_items = [i for i in flat_items if i.get("kind") == "running_skill"]
    run_ids = [i["run_id"] for i in skill_items]
    assert run_ids.index("r-root") < run_ids.index("r-child")
