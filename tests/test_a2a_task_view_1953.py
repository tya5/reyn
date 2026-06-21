"""Tier 2: #1953 slice 5a — A2A view of a reyn Task (status-map + envelope).

The term-neutral Task → A2A Task envelope boundary mapper. Completeness: every
reyn Task state maps to a legal A2A TaskState (drift guard). Envelope shape +
contextId reverse-map (#1814).

Falsification:
- the completeness test reds if any Task state maps to a non-spec A2A state.
- the every-state test reds if a Task state is dropped from the map.
"""
from __future__ import annotations

from reyn.interfaces.web.a2a_task_view import (
    _A2A_TASK_STATES,
    _TASK_STATE_TO_A2A,
    task_state_to_a2a,
    to_a2a_task,
)
from reyn.task.model import Task, TaskOrigin, TaskState


def test_every_task_state_is_mapped():
    """Tier 2: the map covers every reyn TaskState (no state silently unmapped)."""
    mapped = set(_TASK_STATE_TO_A2A)
    all_states = {s.value for s in TaskState}
    # RED if a TaskState is added without a map entry.
    assert mapped == all_states, all_states ^ mapped


def test_map_targets_are_legal_a2a_states():
    """Tier 2: every map target is a legal A2A TaskState (drift guard, #1912b form)."""
    bad = set(_TASK_STATE_TO_A2A.values()) - _A2A_TASK_STATES
    assert bad == set()


def test_key_state_mappings():
    """Tier 2: the load-bearing mappings (Task vocab → A2A), incl. the interim
    blocked→input-required (slice 7 splits it) and abort=delete archived→canceled."""
    assert task_state_to_a2a("in_progress") == "working"
    assert task_state_to_a2a("completed") == "completed"
    assert task_state_to_a2a("failed") == "failed"
    assert task_state_to_a2a("aborted") == "canceled"
    assert task_state_to_a2a("archived") == "canceled"
    assert task_state_to_a2a("blocked") == "input-required"  # interim (slice 7)
    # unknown → safe non-terminal default.
    assert task_state_to_a2a("some-future-state") == "working"


def test_to_a2a_task_envelope_shape():
    """Tier 2: the envelope = {kind:task, id, status:{state,timestamp}, contextId}
    with contextId recovered from the assignee's session_id (A2A reverse map)."""
    task = Task(task_id="t-1", name="n", assignee="a2a:ctx-7", requester="r",
                origin=TaskOrigin.EXTERNAL, status=TaskState.IN_PROGRESS)
    env = to_a2a_task(task)
    assert env["kind"] == "task"
    assert env["id"] == "t-1"
    assert env["status"]["state"] == "working"
    assert "timestamp" in env["status"]
    # contextId = the A2A view of the assignee session_id (term-neutral reverse map).
    assert env["contextId"] == "ctx-7"
    # flat reyn shape does not leak.
    assert "run_id" not in env
    assert not isinstance(env["status"], str)
