"""Task op handlers (#1953 slice 1) — route ``task.*`` Control IR ops to the
Task backend.

Slice 1 uses a process-local in-memory backend (the stub the design calls for);
slice 2 swaps in config-driven sqlite resolution threaded through ``OpContext``.
The single-writer claim token is the caller's ``run_id`` from ``OpContext``
(audit C2) — never an op field, so it cannot be forged by the LLM. Enforcement
(CAS reject, abort quiescence, cascade, cycle-check, predicate-eval) lands in
later slices; these handlers expose the contract surface.
"""
from __future__ import annotations

import uuid

from reyn.task import InMemoryTaskBackend, Task, TaskOrigin

from . import register
from .context import OpContext

# Slice-1 process-local backend. Replaced in slice 2 by config-driven resolution
# (sqlite / in-memory / gh-issue) threaded through OpContext.
_BACKEND: InMemoryTaskBackend = InMemoryTaskBackend()


def _backend() -> InMemoryTaskBackend:
    return _BACKEND


def reset_backend_for_test() -> None:
    """Test hook — reset the slice-1 process-local backend between tests."""
    global _BACKEND
    _BACKEND = InMemoryTaskBackend()


def _ok(kind: str, **data) -> dict:
    return {"kind": kind, "status": "ok", **data}


def _not_found(kind: str, task_id: str) -> dict:
    return {"kind": kind, "status": "error", "error": f"task {task_id!r} not found"}


def _actor(ctx: OpContext) -> str | None:
    """The acting agent identity (audit provenance — created_by / comment author).
    NOT the single-writer token (that is the run_id, threaded separately)."""
    return getattr(ctx, "agent_id", None)


async def _create(op, ctx: OpContext, caller) -> dict:
    task = Task(
        task_id=uuid.uuid4().hex,
        name=op.name,
        assignee=op.assignee,
        requester=op.requester,
        origin=TaskOrigin(op.origin),
        description=op.description,
        budget_cap=op.budget_cap,
        created_by=_actor(ctx),
        deps=list(op.deps),
    )
    created = await _backend().create(task)
    return _ok("task.create", task=created.to_dict())


async def _update_status(op, ctx: OpContext, caller) -> dict:
    # writer_token = caller's run_id (single-writer claim token, audit C2); slice 3
    # CAS-rejects on current_run_id mismatch.
    task = await _backend().update_status(op.task_id, op.status, writer_token=ctx.run_id)
    if task is None:
        return _not_found("task.update_status", op.task_id)
    return _ok("task.update_status", task=task.to_dict())


async def _get(op, ctx: OpContext, caller) -> dict:
    task = await _backend().get(op.task_id)
    if task is None:
        return _not_found("task.get", op.task_id)
    return _ok("task.get", task=task.to_dict())


async def _list(op, ctx: OpContext, caller) -> dict:
    tasks = await _backend().list(
        assignee=op.assignee,
        requester=op.requester,
        status=op.status,
        parent_id=op.parent_id,
    )
    return _ok("task.list", tasks=[t.to_dict() for t in tasks])


async def _create_subtask(op, ctx: OpContext, caller) -> dict:
    parent = await _backend().get(op.parent_id)
    if parent is None:
        return _not_found("task.create_subtask", op.parent_id)
    child = Task(
        task_id=uuid.uuid4().hex,
        name=op.name,
        assignee=op.assignee,
        requester=op.parent_id,  # parent is the child's requester (§16)
        origin=parent.origin,    # lineage inherits origin
        description=op.description,
        parent_id=op.parent_id,
        created_by=_actor(ctx),
        deps=list(op.deps),
    )
    created = await _backend().create(child)
    return _ok("task.create_subtask", task=created.to_dict())


async def _add_dependency(op, ctx: OpContext, caller) -> dict:
    task = await _backend().add_dependency(op.task_id, op.depends_on)
    if task is None:
        return _not_found("task.add_dependency", op.task_id)
    return _ok("task.add_dependency", task=task.to_dict())


async def _abort(op, ctx: OpContext, caller) -> dict:
    task = await _backend().abort(op.task_id, reason=op.reason)
    if task is None:
        return _not_found("task.abort", op.task_id)
    return _ok("task.abort", task=task.to_dict())


async def _archive(op, ctx: OpContext, caller) -> dict:
    task = await _backend().archive(op.task_id)
    if task is None:
        return _not_found("task.archive", op.task_id)
    return _ok("task.archive", task=task.to_dict())


async def _heartbeat(op, ctx: OpContext, caller) -> dict:
    task = await _backend().get(op.task_id)
    if task is None:
        return _not_found("task.heartbeat", op.task_id)
    # slice 7 adds predicate-eval + liveness-timeout; slice 1 reports state.
    return _ok("task.heartbeat", task_id=op.task_id, state=task.status.value, unblocked=False)


async def _register_unblock_predicate(op, ctx: OpContext, caller) -> dict:
    task = await _backend().set_unblock_predicate(op.task_id, op.predicate)
    if task is None:
        return _not_found("task.register_unblock_predicate", op.task_id)
    return _ok("task.register_unblock_predicate", task_id=op.task_id)


async def _comment(op, ctx: OpContext, caller) -> dict:
    comment_id = await _backend().add_comment(op.task_id, _actor(ctx) or "unknown", op.body)
    if comment_id is None:
        return _not_found("task.comment", op.task_id)
    return _ok("task.comment", task_id=op.task_id, comment_id=comment_id)


register("task.create", _create)
register("task.update_status", _update_status)
register("task.get", _get)
register("task.list", _list)
register("task.create_subtask", _create_subtask)
register("task.add_dependency", _add_dependency)
register("task.abort", _abort)
register("task.archive", _archive)
register("task.heartbeat", _heartbeat)
register("task.register_unblock_predicate", _register_unblock_predicate)
register("task.comment", _comment)
