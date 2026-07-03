"""Tier 2: #2107 §16 slice A — recursive-request repr + recovery resolve.

The recursive-request model lets a *task* (not only a session) own a request: a
sub-task created while a session executes a task-as-request T is OWNED by T
(``requester == T.task_id``, ``requester_kind == task``), OS-set from the
execution context (``OpContext.current_task_id``) — never an op field, so the LLM
cannot mark ownership to mis-route a later recovery (the §16 invariant).

Recovery generalizes S1 (route to the requester): when the requester is a TASK,
resolve it to its ASSIGNEE — the managing session that owns + executes the
request — and wake THAT session (one hop; an assignee is always a session).

Tests (real backends + the real TaskWaker / AgentRegistry / route path, no mocks,
no hand-fed requester keys — the ownership comes from the live create-path):

  - full-live-path: a sub-task whose requester is a task-as-request fails → the
    recovery resolves to + wakes the managing session (T's assignee). FALSIFY:
    strip the task→assignee resolve branch → the wake targets a bare task-id with
    no live session → the managing session stays unwoken (RED).
  - create-derivation (both branches): ``current_task_id`` set → requester=that
    task / kind=task; unset → requester=caller-session / kind=session (the
    original model, preserved).
  - sqlite round-trip of the NON-DEFAULT ``requester_kind=task`` (set→reload→get).

KNOWN holes (no-regression in slice A; closed by slice B's persistent assignment):
  (i)  recovery-create — a replacement created on a recovery turn currently falls
       to requester=session (the recovery wake names the failed dependent, not the
       managing task-as-request), so it is not owned by T.
  (ii) multi-turn execution — a sub-task created on a continuation activation (no
       fresh execute-wake) falls to requester=session.
Both only bite slice B's list(requester==T) ownership-cascade; slice A is additive
(parent_id intact, no cascade yet) so a hole-orphan just stays on the S1
session-path = today's behavior.
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
from reyn.runtime.services.task_wake import (
    WAKE_READY_KIND,
    WAKE_REQUESTER_KIND,
    TaskWaker,
)
from reyn.runtime.session import Session
from reyn.task import InMemoryTaskBackend, SqliteTaskBackend, Task, TaskRequesterKind, TaskState
from reyn.task.subscription import SubscriptionRegistry
from tests._support.task_subscription import SubscriptionBackend


def _make_registry(tmp_path: Path) -> AgentRegistry:
    """Real AgentRegistry + on-disk agent + real Session factory (no mocks)."""
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


def _create_op(name, *, deps=None, assignee=None, origin=None):
    return SimpleNamespace(name=name, description=f"do {name}", deps=list(deps or []),
                           assignee=assignee, origin=origin)


def _ctx(backend, *, session_id="executor", current_task_id=None, waker=None):
    return SimpleNamespace(session_id=session_id, agent_id="alice", events=None,
                           task_backend=backend, task_waker=waker,
                           current_task_id=current_task_id)


@pytest.fixture(autouse=True)
def _reset_module_backend():
    taskmod.reset_backend_for_test()
    yield
    taskmod.reset_backend_for_test()


@pytest.mark.asyncio
async def test_subtask_create_derives_task_ownership_from_execution_context():
    """Tier 2: the live create-path derives requester + requester_kind from the
    caller's execution context — NOT from an op field. With current_task_id set the
    sub-task is owned by that task (requester=task, kind=task); unset it is
    session-owned (the original model). No hand-fed requester key."""
    b = InMemoryTaskBackend()

    # executing task-as-request T (no waker → no born-startable wake noise).
    res_sub = await taskmod._create(
        _create_op("sub"), _ctx(b, session_id="executor", current_task_id="T-id"))
    assert res_sub["status"] == "ok"
    sub = await b.get(res_sub["task"]["task_id"])
    # ownership = the executing task, kind=task (the live derivation, not the caller).
    assert sub.requester == "T-id"
    assert sub.requester_kind is TaskRequesterKind.TASK
    # assignee is still the EXECUTOR SESSION (not the task id — the single-writer CAS).
    assert sub.assignee == "executor"

    # a top-level create (no execution context) stays session-owned.
    res_top = await taskmod._create(
        _create_op("top"), _ctx(b, session_id="executor", current_task_id=None))
    top = await b.get(res_top["task"]["task_id"])
    assert top.requester == "executor"
    assert top.requester_kind is TaskRequesterKind.SESSION


@pytest.mark.asyncio
async def test_failed_subtask_recovery_wakes_managing_session_full_live_path(tmp_path):
    """Tier 2: the headline full-live-path. A sub-task U owned by a task-as-request T
    (live create-path) fails with a still-alive dependent → recovery resolves U's TASK
    requester to T's ASSIGNEE (the managing session) and wakes it. Real registry +
    real TaskWaker + real route path; the requester value comes from the live create
    (no hand-feeding). FALSIFY: strip the kind==TASK→assignee resolve branch → the
    notify targets T's bare task-id (no live session) → the managing session stays
    unwoken (the assertion below goes RED)."""
    reg = _make_registry(tmp_path)
    managing = reg.get_or_load("alice")            # the live managing session (sid "main")
    assert managing.inbox.empty()
    b = InMemoryTaskBackend()

    # T = the task-as-request, assigned to (executed by) the live managing session.
    T = Task(task_id="T-req", name="T", assignee="main", requester="a2a:client",
             status=TaskState.RUNNING)
    await b.create(T)

    # U = a sub-task created WHILE executing T → owned by T via the live create-path.
    res_u = await taskmod._create(
        _create_op("U"), _ctx(b, session_id="worker", current_task_id="T-req"))
    u_id = res_u["task"]["task_id"]
    u = await b.get(u_id)
    assert u.requester == "T-req" and u.requester_kind is TaskRequesterKind.TASK  # live value

    # V depends on U (a still-alive dependent), then U fails → recovery must fire.
    await taskmod._create(
        _create_op("V", deps=[u_id]), _ctx(b, session_id="worker"))
    await b.update_status(u_id, TaskState.FAILED, caller_session_id="worker")

    waker = TaskWaker(reg, "alice")
    try:
        await taskmod._route_terminal_to_requester(
            _ctx(b, waker=waker), b, await b.get(u_id), disposition="failed")
        # resolved to T.assignee = the LIVE managing session, and woken there.
        assert not managing.inbox.empty(), "managing session (T.assignee) was not woken"
        kind, payload = managing.inbox.get_nowait()
        assert kind == WAKE_REQUESTER_KIND
        assert u_id in payload["text"]
    finally:
        await _cancel_running(reg)


@pytest.mark.asyncio
async def test_execution_context_threads_through_real_opctx_builder(tmp_path):
    """Tier 2: the #2134 L3 enumerate-all-builders class, applied to the
    SOURCE→builder threading. Stamping the session's per-turn execution context (via
    the real seam, as run_one_iteration does on a ``task_ready`` wake) makes the REAL
    chat op-ctx builder (``Session._make_router_op_context``) carry
    ``current_task_id`` — so a router task.create derives ownership. The recovery wake
    (``task_dependency_aborted``) does NOT stamp (its meta names the failed dependent,
    not the managing task-as-request) → the build stays session-owned (hole (i),
    by-design). Asserts on the BUILT ctx (the public builder output), not private
    state. Falsified by a builder that drops the field (the unit-green/live-broken L3
    failure that cost 3 misses in #2134)."""
    state_log = StateLog(tmp_path / "wal.jsonl")
    s = Session(agent_name="alice", state_log=state_log)

    # execute-wake (task_ready) stamps → the real builder threads it onto the ctx.
    s._stamp_execution_context(WAKE_READY_KIND, {"meta": {"task_id": "T-exec"}})
    ctx_exec = s._make_router_op_context()
    assert ctx_exec.current_task_id == "T-exec"

    # recovery wake (task_dependency_aborted) does NOT stamp → session-owned build.
    s._stamp_execution_context(WAKE_REQUESTER_KIND, {"meta": {"task_id": "U-failed"}})
    ctx_recovery = s._make_router_op_context()
    assert ctx_recovery.current_task_id is None

    # a plain user turn clears it too (per-turn lifetime).
    s._stamp_execution_context("user", {"text": "hi"})
    ctx_user = s._make_router_op_context()
    assert ctx_user.current_task_id is None


@pytest.mark.asyncio
async def test_adapter_builder_threads_current_task_id_live_router_path():
    """Tier 2: #2134 L3 — the LIVE builder. Router-dispatched task ops build their
    op-ctx via RouterHostAdapter.make_router_op_context (NOT the chat
    Session._make_router_op_context), and the adapter reads the per-turn execution
    context through a current_task_id_fn callback (varies per turn, like
    live_session_id_fn). This is the exact builder that dropped task_waker in #2107 —
    so it MUST be CI-pinned for current_task_id too. Mirrors
    test_router_opctx_threads_task_waker_so_chat_abort_wakes_requester. Goes through
    the REAL make_router_op_context. FALSIFY: strip the adapter's current_task_id
    pass-through → ctx.current_task_id is None → RED (the chat-builder test stays
    GREEN — which is exactly why this separate live-builder test is required)."""
    from tests._support.router_host_adapter import make_adapter

    adapter = make_adapter(agent_name="alice", session_id="worker",
                           current_task_id_fn=lambda: "T-exec")
    ctx = adapter.make_router_op_context()
    assert ctx.current_task_id == "T-exec"  # the live-builder wire

    # no execution context (top-level / user turn) → session-owned build.
    adapter_none = make_adapter(agent_name="alice", session_id="worker",
                                current_task_id_fn=lambda: None)
    ctx_none = adapter_none.make_router_op_context()
    assert ctx_none.current_task_id is None


@pytest.mark.asyncio
async def test_requester_kind_task_round_trips_through_sqlite(tmp_path):
    """Tier 2: the NON-DEFAULT requester_kind=task survives a sqlite set→reload→get
    (a default 'session' round-trip would pass trivially even if the binding were
    ignored — this proves persistence)."""
    # #2187 backend-master: requester_kind is the WAL-derived SUBSCRIPTION binding (not a
    # column) — it round-trips through the subscription, hydrated onto the reloaded task.
    db = tmp_path / "tasks.db"
    cp = SubscriptionRegistry()
    b = SubscriptionBackend(SqliteTaskBackend(db, subscription_reader=cp), cp)
    await b.create(Task(task_id="owned", name="owned", assignee="s", requester="T-owner",
                        requester_kind=TaskRequesterKind.TASK))
    b.close()

    # Reopen wired to the SAME control plane (the binding survives via the subscription,
    # separate from the sqlite db) — the requester_kind=task hydrates onto the reloaded task.
    reopened = SqliteTaskBackend(db, subscription_reader=cp)
    got = await reopened.get("owned")
    assert got is not None
    assert got.requester == "T-owner"
    assert got.requester_kind is TaskRequesterKind.TASK
    reopened.close()
