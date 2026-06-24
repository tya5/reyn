"""Tier 2: events-tab curation for the #1953 dynamic task-ops events.

Dogfood-found (live `reyn chat`): the chat-log task events (`task_op`,
`task_readiness`, `task_disposition`, `task_dependency_aborted`) had NO
``_event_hint`` case (blank annotation) and were in NO filter group (couldn't be
isolated; hidden under any non-"all" filter). These pin that each task event
renders a useful hint and that the "task" filter group spans them.

Exercises the public ``_event_hint`` + the ``_FILTER_GROUPS`` table directly —
no mocks, no private state, no golden output.
"""
from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from reyn.interfaces.tui.widgets.right_panel.events_tab import (  # noqa: E402
    _FILTER_GROUPS,
    _event_hint,
)


def _hint(t: str, data: dict) -> str:
    return _event_hint({"type": t, "data": data})


def test_task_op_hint_names_op_and_id() -> None:
    """Tier 2: task_op hint surfaces the op kind + short task id."""
    h = _hint("task_op", {"op": "task.create", "task_id": "d93abb2c4764420c"})
    assert "task.create" in h
    assert "d93abb2c" in h


def test_task_readiness_hint_shows_transition() -> None:
    """Tier 2: task_readiness hint shows the id and the target state."""
    h = _hint("task_readiness", {"task_id": "ea51deed3056", "to": "ready"})
    assert "ea51deed" in h
    assert "ready" in h


def test_task_disposition_hint_shows_disposition() -> None:
    """Tier 2: task_disposition hint surfaces the terminal disposition + id."""
    h = _hint("task_disposition", {"disposition": "aborted", "task_id": "b4ee8c67abcd"})
    assert "aborted" in h
    assert "b4ee8c67" in h


def test_task_dependency_aborted_hint_counts_stuck_dependents() -> None:
    """Tier 2: task_dependency_aborted hint surfaces how many dependents are stuck
    (the recovery-relevant signal)."""
    h = _hint("task_dependency_aborted", {
        "disposition": "aborted",
        "dependents": ["ea51deed3056", "fdff67f6e500"],
    })
    assert "aborted" in h
    assert "2" in h


def test_task_events_are_filterable() -> None:
    """Tier 2: the four chat-log task events live in the "task" filter group, and
    task_dependency_aborted also surfaces under "error" (stuck-dependent signal)."""
    groups = {label: members for label, members in _FILTER_GROUPS}
    assert "task" in groups
    for t in ("task_op", "task_readiness", "task_disposition", "task_dependency_aborted"):
        assert t in groups["task"], f"{t} missing from the task filter group"
    assert "task_dependency_aborted" in groups["error"]


def test_unknown_task_event_does_not_crash() -> None:
    """Tier 2: an unrecognized type still returns a string (blank), never raises."""
    assert isinstance(_hint("task_some_future_event", {"x": 1}), str)
