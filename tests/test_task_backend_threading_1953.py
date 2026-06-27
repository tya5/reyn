"""Tier 2: #1953 slice 3a — Task backend threading + config selection.

Proves the prod-wiring: the session-scoped Task backend reaches the op handlers
on **both** ctx-build seams (ControlIRExecutor + PreprocessorExecutor — the
completeness gate), the handlers use it (a real sqlite backend, not the in-memory
fallback), and the single-writer CAS holds end-to-end through the op layer.

Falsification:
- the completeness tests red if either executor stops propagating task_backend to
  the OpContext (a silent prod-wiring gap — ops would hit the in-memory fallback).
- the CAS e2e test reds if a second caller's run_id is allowed to write.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from reyn.core.events.events import EventLog
from reyn.core.kernel.control_ir_executor import ControlIRExecutor
from reyn.core.kernel.preprocessor_executor import PreprocessorExecutor
from reyn.core.op_runtime import task as taskmod
from reyn.data.workspace.workspace import Workspace
from reyn.security.permissions.permissions import PermissionDecl
from reyn.task import SqliteTaskBackend, create_task_backend
from reyn.task.subscription import SubscriptionRegistry
from tests._support.task_subscription import SubscriptionBackend


def _events_workspace():
    events = EventLog()
    return events, Workspace(events=events)


# ── completeness gate: BOTH ctx-build seams thread task_backend ──────────────


def test_control_ir_executor_threads_task_backend_and_session_to_opcontext():
    """Tier 2: ControlIRExecutor._build_ctx propagates task_backend AND session_id
    to OpContext (the single-writer key rides the same chain)."""
    events, ws = _events_workspace()
    sentinel = object()
    ex = ControlIRExecutor(
        workspace=ws, events=events, permission_resolver=None,
        skill_name="s", chain_id="c", task_backend=sentinel, session_id="sess-1",
    )
    ctx = ex._build_ctx(PermissionDecl(), "phase-1")
    # RED if the control-IR leg drops task_backend (ops would hit the fallback)
    # or session_id (the single-writer CAS would mis-key).
    assert ctx.task_backend is sentinel
    assert ctx.session_id == "sess-1"


def test_preprocessor_executor_threads_task_backend_and_session_to_opcontext():
    """Tier 2: PreprocessorExecutor._build_op_ctx propagates task_backend AND
    session_id (the parallel ctx-build seam — the completeness gate's second half)."""
    events, ws = _events_workspace()
    sentinel = object()
    skill = SimpleNamespace(name="s", permissions=PermissionDecl())
    ex = PreprocessorExecutor(
        skill=skill, workspace=ws, model="standard", events=events,
        subscribers=[], resolver=SimpleNamespace(), task_backend=sentinel,
        session_id="sess-1",
    )
    ctx = ex._build_op_ctx(SimpleNamespace(name="phase-1"), 0)
    # RED if the preprocessor leg drops task_backend or session_id.
    assert ctx.task_backend is sentinel
    assert ctx.session_id == "sess-1"


# ── handler consumes ctx.task_backend (real sqlite, not the fallback) ────────


@pytest.mark.asyncio
async def test_handler_uses_threaded_sqlite_backend(tmp_path: Path):
    """Tier 2: a task op with a sqlite backend on the ctx lands in sqlite, not the
    in-memory fallback."""
    taskmod.reset_backend_for_test()  # ensure the fallback is empty
    # #2187 backend-master: the assignee is the WAL-derived SUBSCRIPTION binding (not a
    # column) — wire the backend's read-through + record the create's binding via the
    # op-mimic wrapper, so the assertion below reads the real binding (hydrated on get).
    cp = SubscriptionRegistry()
    backend = SubscriptionBackend(SqliteTaskBackend(str(tmp_path / "tasks.db"), subscription_reader=cp), cp)
    ctx = SimpleNamespace(task_backend=backend, session_id="sess-1", agent_id="alice", events=None)

    created = await taskmod._create(
        SimpleNamespace(name="n", assignee="bob", requester="alice",
                        origin="self", description=None, deps=[]),
        ctx, "control_ir",
    )
    task_id = created["task"]["task_id"]

    # The task landed in the threaded sqlite backend — proving the handler used
    # ctx.task_backend (had it used the in-memory fallback, sqlite would be empty).
    # The binding (assignee) is hydrated from the subscription the op-mimic recorded.
    got = await backend.get(task_id)
    assert got is not None
    assert got.assignee == "bob"
    backend.close()


@pytest.mark.asyncio
async def test_cas_reject_end_to_end_through_op_layer(tmp_path: Path):
    """Tier 2: the single-writer CAS holds through the op handler — only the
    assignee session (ctx.session_id == assignee) can write."""
    # #2187 backend-master: the CAS is now OP-LAYER ownership-gating — the op's _authorize
    # reads the assignee from the WAL-subscription binding (hydrated on the fetched task) and
    # returns a "denied" result for a non-assignee (no backend PermissionError raise). Wire
    # the backend's read-through + record the binding via the op-mimic wrapper so _authorize
    # reads the real binding.
    cp = SubscriptionRegistry()
    backend = SubscriptionBackend(SqliteTaskBackend(str(tmp_path / "tasks.db"), subscription_reader=cp), cp)
    # ctx_a's session is the assignee; ctx_b is a different session.
    ctx_a = SimpleNamespace(task_backend=backend, session_id="sess-A", agent_id="a", events=None)
    ctx_b = SimpleNamespace(task_backend=backend, session_id="sess-B", agent_id="b", events=None)

    created = await taskmod._create(
        SimpleNamespace(name="n", assignee="sess-A", requester="r",
                        origin="self", description=None, deps=[]),
        ctx_a, "control_ir",
    )
    task_id = created["task"]["task_id"]

    # the assignee session writes.
    ok = await taskmod._update_status(
        SimpleNamespace(task_id=task_id, status="in_progress", reason=None), ctx_a, "control_ir")
    assert ok["status"] == "ok"

    # a non-assignee session is rejected by the CAS — now the op-layer returns a
    # decision-enabling "denied" result (the single-writer gate moved from the backend
    # raise to the op's _authorize binding-check). Same accept/reject semantics preserved.
    denied = await taskmod._update_status(
        SimpleNamespace(task_id=task_id, status="failed", reason=None), ctx_b, "control_ir")
    assert denied["status"] == "denied"
    backend.close()


# ── config-driven selection ─────────────────────────────────────────────────


def test_create_task_backend_selects_by_kind(tmp_path: Path):
    """Tier 2: the factory picks sqlite vs in-memory by config kind; unknown is loud."""
    from reyn.task import InMemoryTaskBackend
    assert isinstance(create_task_backend("in-memory"), InMemoryTaskBackend)
    sq = create_task_backend("sqlite", path=str(tmp_path / "t.db"))
    assert isinstance(sq, SqliteTaskBackend)
    sq.close()
    with pytest.raises(ValueError):
        create_task_backend("sqlite")  # missing path
    with pytest.raises(ValueError):
        create_task_backend("bogus")
