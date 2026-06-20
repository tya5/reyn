"""Tier 2: #1811 — Reyn run-status → A2A TaskState mapping + A2A Task envelope.

``_to_a2a_task`` renders a RunEntry as a spec-shaped A2A Task (§4.1.1): ``id`` +
nested ``status.{state,timestamp,message}`` + optional ``contextId`` / ``artifacts``.
``_a2a_task_state`` maps Reyn's free-form status vocabulary to the JSON-RPC
short-form TaskState; unknown statuses pass through (never crash). Real RunEntry,
no mocks.

Falsification:
- the mapping table test reds if cancelled→canceled (spelling), running→working,
  or timeout→failed regress.
- the completeness test reds if any _STATUS_MAP value is not a legal TaskState.
- the envelope test reds if result is not lifted to artifacts / error not lifted
  to status.message / the flat reyn shape leaks.
"""
from __future__ import annotations

from datetime import datetime, timezone

from reyn.interfaces.web.routers.a2a import (
    _A2A_TASK_STATES,
    _STATUS_MAP,
    _a2a_task_state,
    _to_a2a_task,
)
from reyn.interfaces.web.run_registry import RunEntry


def _entry(**kw) -> RunEntry:
    base = dict(run_id="r-1", agent_name="a", chain_id="c")
    base.update(kw)
    return RunEntry(**base)


def test_status_map_known_reyn_states_map_to_expected_taskstate():
    """Tier 2: each Reyn run status maps to its A2A TaskState (incl. the
    cancelled→canceled spelling fix and the spec-external timeout→failed)."""
    expected = {
        "running": "working",
        "in-progress": "working",
        "completed": "completed",
        "failed": "failed",
        "cancelled": "canceled",
        "input-required": "input-required",
        "timeout": "failed",
    }
    actual = {reyn: _a2a_task_state(reyn) for reyn in expected}
    # RED if any mapping regresses (e.g. cancelled→"cancelled" or timeout passthrough).
    assert actual == expected


def test_status_map_values_are_all_legal_taskstates():
    """Tier 2: completeness — every _STATUS_MAP target is a legal A2A TaskState
    (mirrors the #1912b op-kind-alias value-drift guard)."""
    unknown_targets = set(_STATUS_MAP.values()) - _A2A_TASK_STATES
    # RED if a mapping points at a typo'd / non-spec state.
    assert unknown_targets == set()


def test_a2a_task_state_passes_unknown_through():
    """Tier 2: an unmapped status passes through verbatim (never-hit safety net),
    not crash or silently mislabel."""
    assert _a2a_task_state("some-future-state") == "some-future-state"


def test_to_a2a_task_envelope_shape():
    """Tier 2: a RunEntry renders as a spec A2A Task — id, nested status.state,
    kind=task — and the flat reyn shape does not leak."""
    e = _entry(status="running")
    task = _to_a2a_task(e)

    assert task["kind"] == "task"
    assert task["id"] == "r-1"
    assert task["status"]["state"] == "working"
    assert "timestamp" in task["status"]
    # RED if the flat reyn shape leaks (run_id / flat string status).
    assert "run_id" not in task
    assert not isinstance(task["status"], str)


def test_to_a2a_task_lifts_result_to_artifacts():
    """Tier 2: RunEntry.result (no home in a spec Task) becomes an output Artifact."""
    e = _entry(status="completed", result="the output")
    task = _to_a2a_task(e)

    assert task["status"]["state"] == "completed"
    arts = task["artifacts"]
    assert arts[0]["parts"][0]["text"] == "the output"
    # RED if result were left as a flat top-level field instead.
    assert "result" not in task


def test_to_a2a_task_lifts_error_to_status_message():
    """Tier 2: RunEntry.error becomes the TaskStatus.message note."""
    e = _entry(status="failed", error="boom")
    task = _to_a2a_task(e)

    assert task["status"]["state"] == "failed"
    assert task["status"]["message"]["parts"][0]["text"] == "boom"


def test_to_a2a_task_recovers_contextid_from_session_id():
    """Tier 2: contextId is the A2A-layer reverse map of the core session_id
    (the contextId term never lives in core — #1814)."""
    e = _entry(status="running", session_id="a2a:ctx-42")
    task = _to_a2a_task(e)

    assert task["contextId"] == "ctx-42"
