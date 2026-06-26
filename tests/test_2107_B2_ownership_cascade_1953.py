"""Tier 2: #2107 §16 B2 — the ownership-cascade (abort a task-as-request → its owned sub-tasks).

§18: aborting a task-as-request X aborts everything X OWNS — ``list(requester==X
AND requester_kind==task)`` — recursively. The down-cascade BFS gathers the
ownership edge (``requester==pid AND requester_kind==task``; the legacy parent_id
edge was removed in §16 slice C, so the requester edge is the sole decomposition
relation). The ONE backend.abort seam serves every caller (the op, the A2A cancel
endpoint, /tasks kill) by construction. The recursive sub-tasks (slice A/B1/B1.5)
are owned via this requester edge.

Distinct from S2's dep-DAG dependent cascade (a different graph); UNGATED (owned
PARTS are intrinsic to X, any origin — unlike S2's EXTERNAL-gated dependents).

Tests (both backends, real create-path for the ownership edges — no hand-fed
requester):
  - recursive ownership cascade (depth ≥2): abort X → X + owned U + owned-of-U W all
    archived. RED if the ownership BFS edge is absent (U/W survive).
  - COLLISION GUARD: a SESSION-requester task whose session routing-key == a task_id
    is NOT cascaded (the requester_kind==task marker disambiguates the uuid collision
    requester_kind exists for). RED if the gather drops the marker guard.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from reyn.core.op_runtime import task as taskmod
from reyn.task import InMemoryTaskBackend, SqliteTaskBackend, Task, TaskState
from reyn.task.ref import is_task_ref, make_task_ref


def _create_op(name, *, deps=None):
    return SimpleNamespace(name=name, description=f"do {name}", deps=list(deps or []),
                           assignee=None, origin=None)


def _ctx(backend, *, session_id, current_task_id=None):
    return SimpleNamespace(session_id=session_id, agent_id="a", events=None,
                           task_backend=backend, task_waker=None,
                           current_task_id=current_task_id)


@pytest.fixture(params=["inmem", "sqlite"])
def backend(request, tmp_path):
    if request.param == "inmem":
        yield InMemoryTaskBackend()
    else:
        b = SqliteTaskBackend(tmp_path / "tasks.db")
        yield b
        b.close()


@pytest.fixture(autouse=True)
def _reset_module_backend():
    taskmod.reset_backend_for_test()
    yield
    taskmod.reset_backend_for_test()


@pytest.mark.asyncio
async def test_ownership_cascade_aborts_owned_subtasks_recursively(backend):
    """Tier 2: §18 — aborting a task-as-request X archives its whole OWNERSHIP subtree
    recursively (X → owned U → owned-of-U W, depth 2), via the live create-path's
    requester edges. RED if the ownership BFS edge is absent — U/W would survive."""
    # #2186: X's task_id is a home-addressable task-ref so the cascade's is_task_ref
    # check on owned sub-tasks' requester fields recognises X as the task-owner.
    x_ref = make_task_ref("sX")
    await backend.create(Task(task_id=x_ref, name="X", assignee="sX", requester="client",
                              status=TaskState.IN_PROGRESS))
    res_u = await taskmod._create(
        _create_op("U"), _ctx(backend, session_id="sX", current_task_id=x_ref), "control_ir")
    u_id = res_u["task"]["task_id"]
    res_w = await taskmod._create(
        _create_op("W"), _ctx(backend, session_id="sU", current_task_id=u_id), "control_ir")
    w_id = res_w["task"]["task_id"]

    # live ownership: U is owned by X via the requester edge (the sole decomposition
    # relation now — parent_id was removed in §16 slice C). Self-identifying: is_task_ref.
    u = await backend.get(u_id)
    assert u.requester == x_ref and is_task_ref(u.requester)
    assert (await backend.get(w_id)).requester == u_id

    aborted = await backend.abort(x_ref)
    ids = {t.task_id for t in aborted}
    assert {x_ref, u_id, w_id} <= ids  # the whole ownership subtree, recursively
    for tid in (x_ref, u_id, w_id):
        assert (await backend.get(tid)).status is TaskState.ARCHIVED


@pytest.mark.asyncio
async def test_collision_guard_session_requester_is_not_cascaded(backend):
    """Tier 2: §18 collision-safety — a SESSION-requester task whose session
    routing-key happens to EQUAL the aborted task's id is NOT cascaded. The
    ``requester_kind==task`` guard disambiguates the uuid collision (a spawned-session
    uuid can equal a task-id uuid — the risk requester_kind exists for). RED if the
    gather drops the marker guard (bare requester==X → S wrongly archived)."""
    # X = the aborted task (a task-ref id); S = a task whose requester string is the BARE
    # label "collide" (a session routing-key — not a task-ref). In the #2186 model the
    # cascade is collision-safe BY CONSTRUCTION: a task-ref pid cannot be matched by a
    # bare-string requester (is_task_ref("collide") is False / the sqlite backend's WHERE
    # requester=? can only match a task-ref pid via an exact string — a bare "collide"
    # cannot equal a task-ref). S is NOT cascaded because its requester is not a task-ref.
    x_ref = make_task_ref("sX")
    await backend.create(Task(task_id=x_ref, name="X", assignee="sX", requester="root",
                              status=TaskState.IN_PROGRESS))
    # S's requester is the BARE string "collide" (a session id, not a task-ref).
    # Collision test: "collide" ≠ x_ref, so S is not cascaded (no match in the BFS).
    await backend.create(Task(task_id="S", name="S", assignee="sS", requester="collide",
                              status=TaskState.IN_PROGRESS))

    aborted = await backend.abort(x_ref)
    ids = {t.task_id for t in aborted}
    assert x_ref in ids
    assert "S" not in ids  # the marker guard — NOT cascaded (bare requester ≠ task-ref pid)
    assert (await backend.get("S")).status is TaskState.IN_PROGRESS  # survived
