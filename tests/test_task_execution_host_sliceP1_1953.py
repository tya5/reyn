"""Tier 2: #1953 slice P1 — the narrowed-LLM execution engine relocation.

P1 is a byte-identical MOVE: `_PlanStepHost` relocates out of `planner.py` into
`runtime/task_execution.py` as `TaskExecutionHost` (the Task-execution-engine home,
where P2 generalizes it to run a Task). The planner keeps driving it under its
historical name via a coexist adapter — so plan behavior is unchanged (proven by
the 140 existing planner tests). This test pins the relocation contract.
"""
from __future__ import annotations

from types import SimpleNamespace


class _FakeParent:
    """A real (non-mock) parent host exposing the family data the engine narrows."""

    def list_available_skills(self):
        return [{"name": "code_review", "description": "x"}]

    def list_available_agents(self):
        return []

    def get_file_permissions(self):
        return {"read": ["/repo"]}


def test_engine_relocated_to_task_execution_module():
    """Tier 2: the engine lives in `runtime/task_execution.py` as `TaskExecutionHost`."""
    from reyn.runtime.task_execution import TaskExecutionHost
    assert TaskExecutionHost.__module__ == "reyn.runtime.task_execution"


def test_planner_drives_it_via_coexist_alias():
    """Tier 2: the planner keeps constructing it under `_PlanStepHost` (the coexist
    adapter — the SAME class, so plan behavior is byte-identical)."""
    from reyn.runtime.planner import _PlanStepHost
    from reyn.runtime.task_execution import TaskExecutionHost
    assert _PlanStepHost is TaskExecutionHost


def test_relocated_engine_narrowing_still_works():
    """Tier 2: the relocated engine's narrowing is intact (a `skill__*` unit plumbs
    skills via the moved qualified-name family-gate; #1984 logic moved verbatim)."""
    from reyn.runtime.task_execution import TaskExecutionHost
    host = TaskExecutionHost(
        plan=None, step=SimpleNamespace(tools=["skill__code_review"]),
        prior_results={}, parent=_FakeParent(),
    )
    assert host.list_available_skills()              # plumbed (qualified name)
    # an unrelated unit still silences the family (the per-unit narrowing is preserved).
    bare = TaskExecutionHost(
        plan=None, step=SimpleNamespace(tools=["compact_context"]),
        prior_results={}, parent=_FakeParent(),
    )
    assert bare.list_available_skills() == []
