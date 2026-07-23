"""Tier 2: #2839 Phase 1 — A2A view of a ``RunEntry`` (status-map + envelope).

The A2A layer's boundary mapper from ``RunEntry`` (A2A's own flat run-registry
entry, term-neutral) to an A2A Task envelope. Completeness: every ``RunStatus``
member maps to a legal A2A TaskState (drift guard). Envelope shape + contextId
reverse-map (#1814).

Replaces the pre-#2839 version of this file, which pinned the same contract
against the internal reyn ``Task`` model (``_TASK_STATE_TO_A2A``) — Phase 1
re-bases A2A's GetTask/Cancel authority onto ``RunRegistry``, retiring that
Task-backed mapper (see ``a2a_task_view.py`` module docstring for why: the
Task-backed version had to overload ``blocked`` for ``input-required``, an
interim placeholder its own in-tree comment flagged as never having received
its promised slice-7 fix).

Falsification:
- the completeness test reds if any RunStatus is dropped from the map.
- the every-state test reds if a map target is a non-spec A2A state.
"""
from __future__ import annotations

from datetime import datetime, timezone

from reyn.interfaces.web.a2a_task_view import (
    _A2A_TASK_STATES,
    _RUN_STATUS_TO_A2A,
    run_status_to_a2a,
    to_a2a_task,
)
from reyn.interfaces.web.run_registry import RunEntry, RunStatus


def test_every_run_status_is_mapped():
    """Tier 2: the map covers every RunStatus member (no state silently unmapped)."""
    mapped = set(_RUN_STATUS_TO_A2A)
    all_statuses = set(RunStatus)
    # RED if a RunStatus is added without a map entry.
    assert mapped == all_statuses, all_statuses ^ mapped


def test_map_targets_are_legal_a2a_states():
    """Tier 2: every map target is a legal A2A TaskState (drift guard, #1912b form)."""
    bad = set(_RUN_STATUS_TO_A2A.values()) - _A2A_TASK_STATES
    assert bad == set()


def test_key_state_mappings():
    """Tier 2: the load-bearing mappings (RunStatus → A2A), incl. input-required
    NATIVELY (no blocked-overload — the #2839 Phase 1 correctness fix)."""
    assert run_status_to_a2a(RunStatus.RUNNING) == "working"
    assert run_status_to_a2a(RunStatus.COMPLETED) == "completed"
    assert run_status_to_a2a(RunStatus.FAILED) == "failed"
    assert run_status_to_a2a(RunStatus.CANCELLED) == "canceled"
    assert run_status_to_a2a(RunStatus.INPUT_REQUIRED) == "input-required"


def test_to_a2a_task_envelope_shape():
    """Tier 2: the envelope = {kind:task, id, status:{state,timestamp}, contextId}
    with contextId recovered from the run's session_id (A2A reverse map)."""
    entry = RunEntry(
        run_id="t-1", agent_name="demo", chain_id="c-1",
        status=RunStatus.RUNNING, session_id="a2a:ctx-7",
        updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    env = to_a2a_task(entry)
    assert env["kind"] == "task"
    assert env["id"] == "t-1"
    assert env["status"]["state"] == "working"
    assert "timestamp" in env["status"]
    # contextId = the A2A view of the run's session_id (term-neutral reverse map).
    assert env["contextId"] == "ctx-7"


def test_to_a2a_task_omits_context_id_when_no_session_id():
    """Tier 2: a run with no session_id omits contextId rather than guessing."""
    entry = RunEntry(run_id="t-2", agent_name="demo", chain_id="c-2")
    env = to_a2a_task(entry)
    assert "contextId" not in env
