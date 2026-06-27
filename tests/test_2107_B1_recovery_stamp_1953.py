"""Tier 2: #2107 §16 B1 — recovery-create is owned by the managing task-as-request.

Slice A left hole (i): a session recovering a task-as-request T's failed sub-task by
CREATING a replacement got requester=session (the recovery wake names the FAILED
dependent, not T), so the replacement was an ORPHAN — not owned/aborted with T. B1
closes it (Option A, per-wake + interleaving-precise): the recovery wake
(``task_dependency_aborted``) ALSO carries the managing task T (computed at the
resolve site: requester_kind==TASK → owner), and ``_stamp_execution_context`` stamps
current_task=T for that recovery turn → a replacement created on it is OWNED by T.

Full-live-path (real registry + real TaskWaker + real route + the real adapter
builder; no mocks, no hand-fed keys — ownership comes from the live create-path):
route → the wake meta carries managing_task_id=T → stamp → a recovery-create through
the REAL adapter op-ctx is owned by T. FALSIFY: strip the managing_task_id carry →
the wake meta lacks it → stamp gets None → the replacement falls to requester=session
(the slice-A orphan) → RED.

Interleaving-precise: the recovery wake is task-specific (for T), so stamping
current=T cannot leak into another task's turn (the reason Option A beats a
persistent session-assignment — see the design-call flow-trace).
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from reyn.core.events.state_log import StateLog
from reyn.core.op_runtime import task as taskmod
from reyn.runtime.profile import AgentProfile
from reyn.runtime.registry import AgentRegistry
from reyn.runtime.services.task_wake import WAKE_REQUESTER_KIND, TaskWaker
from reyn.runtime.session import Session
from reyn.task import InMemoryTaskBackend, Task, TaskRequesterKind, TaskState
from tests._support.router_host_adapter import make_adapter


def _make_registry(tmp_path: Path) -> AgentRegistry:
    state_log = StateLog(tmp_path / "wal.jsonl")

    def _factory(profile: AgentProfile) -> Session:
        s = Session(agent_name=profile.name, state_log=state_log)
        s.register_intervention_listener("test")
        return s

    reg = AgentRegistry(project_root=tmp_path, session_factory=_factory, state_log=state_log)
    AgentProfile.new("alice", role="").save(tmp_path / ".reyn" / "agents" / "alice")
    return reg


async def _cancel_running(reg: AgentRegistry) -> None:
    tasks = reg.running_tasks()
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


def _create_op(name, *, deps=None):
    return SimpleNamespace(name=name, description=f"do {name}", deps=list(deps or []),
                           assignee=None, origin=None)


def _ctx(backend, *, session_id="worker", current_task_id=None, waker=None):
    return SimpleNamespace(session_id=session_id, agent_id="alice", events=None,
                           task_backend=backend, task_waker=waker,
                           current_task_id=current_task_id)


@pytest.fixture(autouse=True)
def _reset_module_backend():
    taskmod.reset_backend_for_test()
    yield
    taskmod.reset_backend_for_test()


@pytest.mark.asyncio
async def test_recovery_create_on_task_as_request_is_owned_by_it_full_live_path(tmp_path):
    """Tier 2: §16 B1 — the headline. A sub-task U owned by a task-as-request T
    (live create) fails with a stuck dependent → recovery wakes T's managing session,
    the wake carries managing_task_id=T, the stamp sets current=T, and a REPLACEMENT
    the managing session creates (through the REAL adapter builder) is OWNED by T.
    FALSIFY: strip the managing_task_id carry → the replacement falls to
    requester=session (the slice-A orphan) → RED."""
    reg = _make_registry(tmp_path)
    managing = reg.get_or_load("alice")            # the live managing session (sid "main")
    b = InMemoryTaskBackend()

    # T = the task-as-request, executed by the managing session.
    await b.create(Task(task_id="T-req", name="T", assignee="main", requester="a2a:client",
                        status=TaskState.RUNNING))
    # U owned by T (live create-path), a dependent V, then U fails.
    res_u = await taskmod._create(
        _create_op("U"), _ctx(b, session_id="worker", current_task_id="T-req"), "control_ir")
    u_id = res_u["task"]["task_id"]
    assert (await b.get(u_id)).requester == "T-req"  # live ownership, not hand-fed
    await taskmod._create(_create_op("V", deps=[u_id]), _ctx(b, session_id="worker"), "control_ir")
    await b.update_status(u_id, TaskState.FAILED, caller_session_id="worker")

    waker = TaskWaker(reg, "alice")
    try:
        # recovery routes to T's assignee (managing session) + carries the managing task.
        await taskmod._route_terminal_to_requester(
            _ctx(b, waker=waker), b, await b.get(u_id), disposition="failed")
        kind, payload = managing.inbox.get_nowait()
        assert kind == WAKE_REQUESTER_KIND
        assert payload["meta"]["managing_task_id"] == "T-req"  # B1: the wake carries T

        # the managing session enters its recovery turn → stamps current=T (as
        # run_one_iteration does), then creates a REPLACEMENT through the REAL adapter
        # op-ctx builder → the replacement is OWNED by T (hole (i) closed).
        managing._stamp_execution_context(kind, payload)
        adapter = make_adapter(agent_name="alice", task_backend=b, session_id="main",
                               current_task_id_fn=lambda: managing._current_task_id)
        rctx = adapter.make_router_op_context()
        res_rep = await taskmod._create(_create_op("U-replacement"), rctx, "control_ir")
        replacement = await b.get(res_rep["task"]["task_id"])
        assert replacement.requester == "T-req"  # the recovery-create is owned by T
        assert replacement.requester_kind is TaskRequesterKind.TASK
    finally:
        await _cancel_running(reg)


@pytest.mark.asyncio
async def test_session_requester_recovery_stays_session_owned(tmp_path):
    """Tier 2: §16 B1 — the recovery wake for a SESSION-requester carries no managing
    task (managing_task_id=None) → the recovery turn stays session-owned (a top-level
    request's recovery is not mis-attributed to a task). Guards B1 against
    over-stamping the session-requester path (S1 unchanged)."""
    reg = _make_registry(tmp_path)
    managing = reg.get_or_load("alice")
    b = InMemoryTaskBackend()

    # a flat self-task plan whose requester is the session "main" (the S1 path).
    await b.create(Task(task_id="b2", name="b2", assignee="main", requester="main",
                        status=TaskState.RUNNING))
    await b.create(Task(task_id="b3", name="b3", assignee="main", requester="main", deps=["b2"]))
    await b.update_status("b2", TaskState.FAILED, caller_session_id="main")

    waker = TaskWaker(reg, "alice")
    try:
        await taskmod._route_terminal_to_requester(
            _ctx(b, waker=waker), b, await b.get("b2"), disposition="failed")
        kind, payload = managing.inbox.get_nowait()
        assert kind == WAKE_REQUESTER_KIND
        assert payload["meta"]["managing_task_id"] is None  # session-requester → no managing task
        managing._stamp_execution_context(kind, payload)
        # a create on this recovery turn stays session-owned.
        res = await taskmod._create(
            _create_op("fix"), _ctx(b, session_id="main",
                                    current_task_id=managing._current_task_id), "control_ir")
        fix = await b.get(res["task"]["task_id"])
        assert fix.requester == "main"
        assert fix.requester_kind is TaskRequesterKind.SESSION
    finally:
        await _cancel_running(reg)
