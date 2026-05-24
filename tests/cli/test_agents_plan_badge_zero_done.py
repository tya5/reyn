"""Tier 2: plan badge renders for plan_n_done == 0 (H-F4).

Wave-10 follow-up Topic H finding F4 (P2): the badge guard was
``if plan_n_done and plan_n_total`` — Python truthiness treats
``0`` as falsy, so a plan that just activated (= step 1 of N not
yet completed, ``plan_n_done == 0``) silently hid the badge.

User-visible effect: starting a new plan saw NO badge on the
running skill row until at least one step completed. The badge
then "popped in" mid-run, contradicting the conv-pane
``SkillActivityRow`` which DOES show ``[plan 0/5]`` from the
start of a plan (= consistent across surfaces is the intent).

After the fix the guard uses ``plan_n_done is not None``. The
``plan_n_total`` check stays truthy (= a zero-total plan is
degenerate noise; suppression remains correct).

Public surfaces tested:
  - ``plan_n_done == 0`` + ``plan_n_total == 5`` → badge rendered
    as ``[plan 0/5]``
  - ``plan_n_done is None`` → no badge (regression guard for the
    "no plan attached" case)
  - ``plan_n_total == 0`` → no badge (regression guard for the
    degenerate-plan case)
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


class _StubRegistry:
    """Minimal AgentRegistry stand-in carrying the methods render_agents calls.

    Exposes a single ``test-agent`` so the running-skill branch fires
    and the plan badge gate can be exercised. The session-related
    attributes degrade gracefully for fields render_agents reads
    defensively (= try/except wrapping registry calls).
    """

    def __init__(self) -> None:
        self._agents: dict = {}
        self.attached_name = "test-agent"

    def list_names(self) -> list[str]:
        return ["test-agent"]

    def loaded_names(self):  # type: ignore[no-untyped-def]
        return {"test-agent"}

    def last_activity_at(self, name: str):  # type: ignore[no-untyped-def]
        return None

    def message_count(self, name: str) -> int:
        return 0

    def recent_user_message(self, name: str) -> str:
        return ""


def _render_for_skill(plan_n_done, plan_n_total):
    """Render with a single running skill carrying given plan state.

    Returns the plain text rendering of the returned Rich tree.
    """
    from rich.console import Console

    from reyn.chat.tui.widgets.right_panel.agents_tab import render_agents

    exec_state = {
        "run_abcdef12": {
            "agent_name": "test-agent",
            "skill_name": "test_skill",
            "phase": "p1",
            "elapsed_s": 10,
            "phase_visits": 1,
            "plan_n_done": plan_n_done,
            "plan_n_total": plan_n_total,
        },
    }
    renderable, _flat_items, _ys = render_agents(
        registry=_StubRegistry(),
        exec_state=exec_state,
        project_root=None,
        cursor=0,
    )
    out = Console(file=None, record=True, force_terminal=False, width=120)
    out.print(renderable)
    return out.export_text()


def test_plan_badge_renders_when_done_is_zero() -> None:
    """Tier 2: ``plan_n_done == 0`` with valid total → badge visible."""
    out = _render_for_skill(plan_n_done=0, plan_n_total=5)
    assert "[plan 0/5]" in out, (
        f"plan_n_done=0 should render '[plan 0/5]' badge, got:\n{out!r}"
    )


def test_plan_badge_renders_normally_for_in_flight_step() -> None:
    """Tier 2b: mid-plan badge still renders (= original case)."""
    out = _render_for_skill(plan_n_done=2, plan_n_total=5)
    assert "[plan 2/5]" in out


def test_no_badge_when_plan_n_done_is_none() -> None:
    """Tier 2b: no plan attached → no badge.

    ``None`` means the skill isn't part of a plan at all. The fix
    swap from ``and`` to ``is not None`` must still suppress the
    badge for this case.
    """
    out = _render_for_skill(plan_n_done=None, plan_n_total=5)
    assert "plan " not in out or "[plan" not in out, (
        f"None plan_n_done should not render badge, got:\n{out!r}"
    )


def test_no_badge_when_plan_n_total_is_zero() -> None:
    """Tier 2b: degenerate zero-total plan → no badge.

    A plan with 0 total steps is producer-side noise; the badge
    would read ``[plan 0/0]`` which conveys nothing.
    """
    out = _render_for_skill(plan_n_done=0, plan_n_total=0)
    assert "[plan 0/0]" not in out
