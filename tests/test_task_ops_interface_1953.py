"""Tier 1/2: #1953 slice 1 — Task op interface contract surface.

The ``task.*`` Control IR ops exist, validate through the Op union, are
registered + gated (completeness), and round-trip through the in-memory backend.
Enforcement (single-writer CAS, abort quiescence, cascade, cycle-check,
predicate-eval) lands in later slices — this slice is the contract surface.

Falsification:
- completeness test reds if any task op kind is missing from the contextual gate
  (a silent capability bypass) or from the handler registry.
- the writer-token test reds if ``update_status`` stops threading the caller's
  run_id as the single-writer claim token (audit C2).
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import TypeAdapter

from reyn.core.op_runtime import available_kinds
from reyn.core.op_runtime import task as taskmod
from reyn.core.op_runtime.contextual_gate import _OP_KIND_ALIASES
from reyn.schemas.models import ALL_OP_KINDS, OP_KIND_MODEL_MAP, Op
from reyn.task import InMemoryTaskBackend, Task, TaskState
from reyn.task.subscription import SubscriptionRegistry
from tests._support.task_subscription import SubscriptionBackend

_TASK_KINDS = frozenset(k for k in ALL_OP_KINDS if k.startswith("task."))


def _ctx(session_id: str = "sess-1", agent_id: str = "alice"):
    """Minimal OpContext stand-in. session_id is the caller identity (requester on
    create + the role-gate key); the handlers also read agent_id (audit) + events."""
    return SimpleNamespace(session_id=session_id, agent_id=agent_id, events=None)


@pytest.fixture(autouse=True)
def _reset_backend():
    taskmod.reset_backend_for_test()
    yield
    taskmod.reset_backend_for_test()


# ── registry / gate completeness (Tier 1 contract) ──────────────────────────


def test_all_task_kinds_present_in_registry():
    """Tier 1: every task op kind has a model + handler (no half-wiring)."""
    assert _TASK_KINDS  # non-empty; the exact set is pinned in the union test
    handlers = set(available_kinds())
    for kind in _TASK_KINDS:
        assert kind in OP_KIND_MODEL_MAP
        assert kind in handlers


def test_contextual_gate_covers_every_task_kind():
    """Tier 1: the contextual gate enumerates every task kind — a missing entry
    would be a silent capability bypass (the #1912b completeness invariant)."""
    missing = _TASK_KINDS - set(_OP_KIND_ALIASES)
    # RED if a task op is added without a gate entry.
    assert missing == set()


def test_union_validates_every_task_kind():
    """Tier 1: each task op kind round-trips through the Op union."""
    adapter = TypeAdapter(Op)
    samples = {
        "task.create": {"kind": "task.create", "name": "n"},
        "task.update_status": {"kind": "task.update_status", "task_id": "t", "status": "running"},
        "task.get": {"kind": "task.get", "task_id": "t"},
        "task.list": {"kind": "task.list"},
        "task.add_dependency": {"kind": "task.add_dependency", "task_id": "t", "depends_on": "u"},
        "task.remove_dependency": {"kind": "task.remove_dependency", "task_id": "t", "depends_on": "u"},
        "task.repoint_dependency": {"kind": "task.repoint_dependency", "task_id": "t", "from_depends_on": "u", "to_depends_on": "v"},
        "task.abort": {"kind": "task.abort", "task_id": "t"},
        "task.heartbeat": {"kind": "task.heartbeat", "task_id": "t"},
        "task.register_unblock_predicate": {"kind": "task.register_unblock_predicate", "task_id": "t", "predicate": "x"},
        "task.comment": {"kind": "task.comment", "task_id": "t", "body": "hi"},
        "task.assign": {"kind": "task.assign", "task_id": "t", "assignee": "s"},
    }
    # every kind has a sample (forces this test to grow with the op-set)
    assert set(samples) == _TASK_KINDS
    for kind, payload in samples.items():
        op = adapter.validate_python(payload)
        assert op.kind == kind


# ── backend round-trip (Tier 2 — in-memory stub) ────────────────────────────


@pytest.mark.asyncio
async def test_inmemory_backend_create_get_list_roundtrip():
    """Tier 2: a non-default Task round-trips through the in-memory backend."""
    backend = InMemoryTaskBackend()
    task = Task(
        task_id="t1", name="ship", assignee="bob", requester="alice",
        status=TaskState.BLOCKED,
    )
    await backend.create(task)

    got = await backend.get("t1")
    assert got is not None
    assert got.assignee == "bob"
    assert got.status is TaskState.BLOCKED

    by_assignee = await backend.list(assignee="bob")
    assert [t.task_id for t in by_assignee] == ["t1"]
    assert await backend.list(assignee="nobody") == []


# ── handler contract (Tier 2) ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_then_get_via_handlers():
    """Tier 2: task.create returns a task_id that task.get resolves."""
    created = await taskmod._create(
        SimpleNamespace(name="ship", assignee="bob", requester="alice",
                        origin="self", description=None, deps=[]),
        _ctx()
    )
    assert created["status"] == "ok"
    task_id = created["task"]["task_id"]

    got = await taskmod._get(SimpleNamespace(task_id=task_id), _ctx())
    assert got["status"] == "ok"
    assert got["task"]["assignee"] == "bob"


@pytest.mark.asyncio
async def test_update_status_single_writer_is_assignee_session():
    """Tier 2: update_status keys the single-writer on the caller's session_id ==
    the (immutable) assignee — the assignee session writes, a non-assignee is
    rejected (settled #1953 model; run_id/current_run_id retired). #2187 backend-master:
    the CAS is the OP-LAYER gate (the op's _authorize reads the assignee from the
    WAL-subscription binding), returning a decision-enabling "denied" — not a backend raise."""
    # Wire the create + reads through a #2187 subscription-backed backend so the op's
    # _authorize reads the real (WAL-subscription) binding the op-mimic recorded.
    cp = SubscriptionRegistry()
    backend = SubscriptionBackend(InMemoryTaskBackend(subscription_reader=cp), cp)
    created = await taskmod._create(
        SimpleNamespace(name="n", assignee="sess-A", requester="alice",
                        origin="self", description=None, deps=[]),
        SimpleNamespace(session_id="alice", agent_id="a", events=None, task_backend=backend)
    )
    task_id = created["task"]["task_id"]

    # the assignee session (session_id == assignee) may write.
    updated = await taskmod._update_status(
        SimpleNamespace(task_id=task_id, status="running", reason=None),
        SimpleNamespace(session_id="sess-A", agent_id="a", events=None, task_backend=backend)
    )
    assert updated["status"] == "ok"
    assert updated["task"]["status"] == "running"

    # RED if the single-writer CAS is dropped: a non-assignee session is rejected — now
    # the op-layer returns a "denied" result (the gate moved from a backend raise to the
    # op's _authorize binding-check). Same reject semantics preserved.
    denied = await taskmod._update_status(
        SimpleNamespace(task_id=task_id, status="failed", reason=None),
        SimpleNamespace(session_id="sess-B", agent_id="b", events=None, task_backend=backend)
    )
    assert denied["status"] == "denied"


@pytest.mark.asyncio
async def test_abort_emits_disposition_event_per_aborted_task():
    """Tier 2: abort (UP-notify, 2b-2) emits a term-neutral `task_disposition`
    P6 event per aborted task carrying requester + origin — the A2A layer (slice
    5) routes origin=external to the external (webhook) channel. Falsification (d):
    each aborted task's event carries the correct requester + origin."""
    from reyn.core.events.events import EventLog
    from reyn.task import InMemoryTaskBackend, Task, TaskOrigin, TaskRequesterKind

    backend = InMemoryTaskBackend()
    # external root P (origin=external, persistent external requester X) that OWNS an
    # internal sub-task C (requester=p, the §16 ownership edge — parent_id removed in
    # slice C) — abort P cascades to C; both get a disposition event.
    await backend.create(Task(task_id="p", name="p", assignee="A", requester="X",
                              origin=TaskOrigin.EXTERNAL))
    await backend.create(Task(task_id="c", name="c", assignee="A", requester="p",
                              requester_kind=TaskRequesterKind.TASK,
                              origin=TaskOrigin.SELF))
    events = EventLog()
    ctx = SimpleNamespace(task_backend=backend, session_id="X", agent_id="x", events=events)

    res = await taskmod._abort(SimpleNamespace(task_id="p", reason=None), ctx)
    assert res["status"] == "ok"

    disp = {e.data["task_id"]: e.data for e in events.all() if e.type == "task_disposition"}
    # RED if a cascade-aborted task is missing an event, or origin/requester wrong.
    assert set(disp) == {"p", "c"}
    assert disp["p"]["origin"] == "external" and disp["p"]["requester"] == "X"
    assert disp["c"]["origin"] == "self" and disp["c"]["requester"] == "p"
    assert all(d["disposition"] == "aborted" for d in disp.values())


@pytest.mark.asyncio
async def test_abort_archives_and_rejects_assignee_straggler():
    """Tier 2: abort = delete → archived (Option B); a post-abort straggler
    update_status by the assignee is rejected by the terminal state, so nothing
    lands (RED if the terminal-guard is dropped)."""
    created = await taskmod._create(
        SimpleNamespace(name="t", assignee="A", description=None, deps=[]),
        SimpleNamespace(session_id="R", agent_id="r", events=None))
    task_id = created["task"]["task_id"]

    # requester R aborts (= delete) → archived.
    aborted = await taskmod._abort(
        SimpleNamespace(task_id=task_id, reason="don't need it"),
        SimpleNamespace(session_id="R", agent_id="r", events=None))
    assert aborted["task"]["status"] == "aborted"

    # the assignee's straggler write is rejected by the terminal state.
    with pytest.raises(PermissionError):
        await taskmod._update_status(
            SimpleNamespace(task_id=task_id, status="done", reason=None),
            SimpleNamespace(session_id="A", agent_id="a", events=None))


@pytest.mark.asyncio
async def test_handlers_return_error_for_unknown_task():
    """Tier 2: ops on a missing task return a decision-enabling error, not a crash."""
    got = await taskmod._get(SimpleNamespace(task_id="nope"), _ctx())
    assert got["status"] == "error"
    assert "not found" in got["error"]


# ── role-based op authority (P5) ────────────────────────────────────────────


async def _make_cross_session_task(requester="R", assignee="A", *, backend=None):
    """Create a task with requester=R (the caller) and assignee=A (cross-session).
    When ``backend`` is given (a #2187 subscription-wired backend), the create lands
    there and the binding is recorded to its subscription (so the op-layer role-gate
    reads the real WAL-subscription binding); else the module fallback is used."""
    ctx = SimpleNamespace(session_id=requester, agent_id="x", events=None)
    if backend is not None:
        ctx.task_backend = backend
    created = await taskmod._create(
        SimpleNamespace(name="n", assignee=assignee, description=None, deps=[]),
        ctx
    )
    assert created["task"]["requester"] == requester
    assert created["task"]["assignee"] == assignee
    return created["task"]["task_id"]


@pytest.mark.asyncio
async def test_requester_gated_ops_reject_non_requester():
    """Tier 2: get / add_dependency / abort are requester-gated — the assignee
    (non-requester) is denied (role_denied). RED if the gate is dropped."""
    task_id = await _make_cross_session_task(requester="R", assignee="A")
    assignee_ctx = SimpleNamespace(session_id="A", agent_id="a", events=None)

    for handler, op in (
        (taskmod._get, SimpleNamespace(task_id=task_id)),
        (taskmod._add_dependency, SimpleNamespace(task_id=task_id, depends_on="u")),
        (taskmod._abort, SimpleNamespace(task_id=task_id, reason=None)),
    ):
        res = await handler(op, assignee_ctx)
        assert res["status"] == "denied", (handler.__name__, res)
        assert res["error"]["kind"] == "role_denied"

    # the requester itself is allowed.
    req_ctx = SimpleNamespace(session_id="R", agent_id="r", events=None)
    allowed = await taskmod._get(SimpleNamespace(task_id=task_id), req_ctx)
    assert allowed["status"] == "ok"


@pytest.mark.asyncio
async def test_assignee_gated_ops_reject_non_assignee():
    """Tier 2: update_status / heartbeat / register_unblock_predicate are
    assignee-gated — the requester (non-assignee) is denied. RED if the gate drops."""
    # #2187 backend-master: wire a subscription-backed backend so the op's _authorize
    # reads the real (WAL-subscription) binding the op-mimic recorded on create.
    cp = SubscriptionRegistry()
    backend = SubscriptionBackend(InMemoryTaskBackend(subscription_reader=cp), cp)
    task_id = await _make_cross_session_task(requester="R", assignee="A", backend=backend)
    req_ctx = SimpleNamespace(session_id="R", agent_id="r", events=None, task_backend=backend)
    assignee_ctx = SimpleNamespace(session_id="A", agent_id="a", events=None, task_backend=backend)

    # update_status: the op-layer CAS now returns a "denied" result for the non-assignee
    # (the gate moved from a backend PermissionError raise to the op's _authorize binding-
    # check). Same reject semantics preserved.
    denied = await taskmod._update_status(
        SimpleNamespace(task_id=task_id, status="failed", reason=None), req_ctx)
    assert denied["status"] == "denied"
    # heartbeat / register: handler role-gate → denied for the non-assignee.
    for handler, op in (
        (taskmod._heartbeat, SimpleNamespace(task_id=task_id)),
        (taskmod._register_unblock_predicate, SimpleNamespace(task_id=task_id, predicate="x")),
    ):
        res = await handler(op, req_ctx)
        assert res["status"] == "denied", (handler.__name__, res)

    # the assignee itself is allowed.
    allowed = await taskmod._heartbeat(SimpleNamespace(task_id=task_id), assignee_ctx)
    assert allowed["status"] == "ok"
