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


def _events_workspace():
    events = EventLog()
    return events, Workspace(events=events)


# ── completeness gate: BOTH ctx-build seams thread task_backend ──────────────


def test_control_ir_executor_threads_task_backend_to_opcontext():
    """Tier 2: ControlIRExecutor._build_ctx propagates task_backend to OpContext."""
    events, ws = _events_workspace()
    sentinel = object()
    ex = ControlIRExecutor(
        workspace=ws, events=events, permission_resolver=None,
        skill_name="s", chain_id="c", task_backend=sentinel,
    )
    ctx = ex._build_ctx(PermissionDecl(), "phase-1")
    # RED if the control-IR leg drops task_backend (ops would hit the fallback).
    assert ctx.task_backend is sentinel


def test_preprocessor_executor_threads_task_backend_to_opcontext():
    """Tier 2: PreprocessorExecutor._build_op_ctx propagates task_backend (the
    parallel ctx-build seam — the completeness gate's second half)."""
    events, ws = _events_workspace()
    sentinel = object()
    skill = SimpleNamespace(name="s", permissions=PermissionDecl())
    ex = PreprocessorExecutor(
        skill=skill, workspace=ws, model="standard", events=events,
        subscribers=[], resolver=SimpleNamespace(), task_backend=sentinel,
    )
    ctx = ex._build_op_ctx(SimpleNamespace(name="phase-1"), 0)
    # RED if the preprocessor leg drops task_backend.
    assert ctx.task_backend is sentinel


# ── handler consumes ctx.task_backend (real sqlite, not the fallback) ────────


@pytest.mark.asyncio
async def test_handler_uses_threaded_sqlite_backend(tmp_path: Path):
    """Tier 2: a task op with a sqlite backend on the ctx lands in sqlite, not the
    in-memory fallback."""
    taskmod.reset_backend_for_test()  # ensure the fallback is empty
    backend = SqliteTaskBackend(str(tmp_path / "tasks.db"))
    ctx = SimpleNamespace(task_backend=backend, run_id="run-1", agent_id="alice", events=None)

    created = await taskmod._create(
        SimpleNamespace(name="n", assignee="bob", requester="alice",
                        origin="self", description=None, budget_cap=None, deps=[]),
        ctx, "control_ir",
    )
    task_id = created["task"]["task_id"]

    # The task landed in the threaded sqlite backend — proving the handler used
    # ctx.task_backend (had it used the in-memory fallback, sqlite would be empty).
    got = await backend.get(task_id)
    assert got is not None
    assert got.assignee == "bob"
    backend.close()


@pytest.mark.asyncio
async def test_cas_reject_end_to_end_through_op_layer(tmp_path: Path):
    """Tier 2: the single-writer CAS holds through the op handler — a second
    caller (different run_id) cannot write the task."""
    backend = SqliteTaskBackend(str(tmp_path / "tasks.db"))
    ctx_a = SimpleNamespace(task_backend=backend, run_id="run-A", agent_id="a", events=None)
    ctx_b = SimpleNamespace(task_backend=backend, run_id="run-B", agent_id="b", events=None)

    created = await taskmod._create(
        SimpleNamespace(name="n", assignee="bob", requester="r",
                        origin="self", description=None, budget_cap=None, deps=[]),
        ctx_a, "control_ir",
    )
    task_id = created["task"]["task_id"]

    # run-A claims the task.
    ok = await taskmod._update_status(
        SimpleNamespace(task_id=task_id, status="in_progress", reason=None), ctx_a, "control_ir")
    assert ok["status"] == "ok"

    # run-B is rejected by the CAS (PermissionError → execute_op surfaces "denied";
    # the handler raises, mirroring the backend contract).
    with pytest.raises(PermissionError):
        await taskmod._update_status(
            SimpleNamespace(task_id=task_id, status="failed", reason=None), ctx_b, "control_ir")
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
