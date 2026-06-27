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
from reyn.task import InMemoryTaskBackend, SqliteTaskBackend, Task, TaskRequesterKind, TaskState
from reyn.task.subscription import SubscriptionRegistry
from tests._support.task_subscription import SubscriptionBackend


def _create_op(name, *, deps=None):
    return SimpleNamespace(name=name, description=f"do {name}", deps=list(deps or []),
                           assignee=None, origin=None)


def _ctx(backend, *, session_id, current_task_id=None):
    return SimpleNamespace(session_id=session_id, agent_id="a", events=None,
                           task_backend=backend, task_waker=None,
                           current_task_id=current_task_id)


@pytest.fixture(params=["inmem", "sqlite"])
def backend(request, tmp_path):
    # #2187 backend-master: the ownership (requester) edge the cascade follows is the
    # WAL-derived SUBSCRIPTION binding (not a column) — wire each backend's read-through to
    # a SubscriptionRegistry + record each create's binding to it via the op-mimic wrapper,
    # so the down-cascade walks the real binding (whether the op or a direct create made it).
    cp = SubscriptionRegistry()
    if request.param == "inmem":
        yield SubscriptionBackend(InMemoryTaskBackend(subscription_reader=cp), cp)
    else:
        real = SqliteTaskBackend(tmp_path / "tasks.db", subscription_reader=cp)
        yield SubscriptionBackend(real, cp)
        real.close()


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
    await backend.create(Task(task_id="X", name="X", assignee="sX", requester="client",
                              status=TaskState.IN_PROGRESS))
    res_u = await taskmod._create(
        _create_op("U"), _ctx(backend, session_id="sX", current_task_id="X"), "control_ir")
    u_id = res_u["task"]["task_id"]
    res_w = await taskmod._create(
        _create_op("W"), _ctx(backend, session_id="sU", current_task_id=u_id), "control_ir")
    w_id = res_w["task"]["task_id"]

    # live ownership: U is owned by X via the requester edge (the sole decomposition
    # relation now — parent_id was removed in §16 slice C).
    u = await backend.get(u_id)
    assert u.requester == "X" and u.requester_kind is TaskRequesterKind.TASK
    assert (await backend.get(w_id)).requester == u_id

    aborted = await backend.abort("X")
    ids = {t.task_id for t in aborted}
    assert {"X", u_id, w_id} <= ids  # the whole ownership subtree, recursively
    for tid in ("X", u_id, w_id):
        assert (await backend.get(tid)).status is TaskState.ARCHIVED


@pytest.mark.asyncio
async def test_collision_guard_session_requester_is_not_cascaded(backend):
    """Tier 2: §18 collision-safety — a SESSION-requester task whose session
    routing-key happens to EQUAL the aborted task's id is NOT cascaded. The
    ``requester_kind==task`` guard disambiguates the uuid collision (a spawned-session
    uuid can equal a task-id uuid — the risk requester_kind exists for). RED if the
    gather drops the marker guard (bare requester==X → S wrongly archived)."""
    # X = the aborted task; S = a SESSION-requester whose requester string == X's id.
    await backend.create(Task(task_id="collide", name="X", assignee="sX", requester="root",
                              status=TaskState.IN_PROGRESS))
    await backend.create(Task(task_id="S", name="S", assignee="sS", requester="collide",
                              requester_kind=TaskRequesterKind.SESSION,
                              status=TaskState.IN_PROGRESS))

    aborted = await backend.abort("collide")
    ids = {t.task_id for t in aborted}
    assert "collide" in ids
    assert "S" not in ids  # the marker guard — NOT cascaded despite the id collision
    assert (await backend.get("S")).status is TaskState.IN_PROGRESS  # survived
