"""Task op handlers (#1953) — route ``task.*`` Control IR ops to the Task backend.

The backend is resolved from ``OpContext.task_backend`` (the session-scoped,
config-selected backend threaded in slice 3a) with an in-memory fallback for
tests / direct construction. The single-writer claim token is the caller's
``run_id`` from ``OpContext`` (audit C2) — never an op field, so the LLM cannot
forge it; the sqlite backend CAS-rejects on a mismatch (slice 2). Each mutating
handler also emits a generic P6 audit event (``task_op``); the backend's own
``task_events`` is the source of truth (the WAL closed vocab is not expanded, P7).

Still deferred: abort quiescence 3-step (slice 3b), cascade, cycle-check,
predicate-eval (later slices).
"""
from __future__ import annotations

import uuid

from reyn.task import InMemoryTaskBackend, Task, TaskOrigin

from . import register
from .context import OpContext

# Process-local in-memory fallback backend. Used when OpContext carries no
# session-scoped backend (tests / direct OpContext construction / CLI). Slice 3a
# threads the real (config-selected, session-scoped) backend on ``ctx.task_backend``.
_BACKEND: InMemoryTaskBackend = InMemoryTaskBackend()


def _backend(ctx: OpContext):
    """Resolve the Task backend: the session-scoped one threaded on the
    OpContext (#1953 slice 3a) when present, else the in-memory fallback."""
    return getattr(ctx, "task_backend", None) or _BACKEND


def reset_backend_for_test() -> None:
    """Test hook — reset the process-local in-memory fallback between tests."""
    global _BACKEND
    _BACKEND = InMemoryTaskBackend()


def _audit(ctx: OpContext, op_kind: str, task_id: str, **fields) -> None:
    """P6 audit emit for a task op (generic type; the backend's own task_events
    stays the source of truth — the WAL ``state_log`` closed vocab is NOT
    expanded, P7). No-op when the context has no event log (direct construction)."""
    events = getattr(ctx, "events", None)
    if events is not None:
        events.emit("task_op", op=op_kind, task_id=task_id, **fields)


def _ok(kind: str, **data) -> dict:
    return {"kind": kind, "status": "ok", **data}


def _not_found(kind: str, task_id: str) -> dict:
    return {"kind": kind, "status": "error", "error": f"task {task_id!r} not found"}


def _actor(ctx: OpContext) -> str | None:
    """The acting agent identity (audit provenance — created_by / comment author)."""
    return getattr(ctx, "agent_id", None)


def _caller_session(ctx: OpContext) -> str | None:
    """The caller's session identity (OpContext.session_id, the #1814 routing-key)
    — the key for role-based op authority (assignee / requester gating)."""
    return getattr(ctx, "session_id", None)


def _role_denied(op_kind: str, task_id: str, role: str, caller: str | None) -> dict:
    """Decision-enabling denied result for a role-gated op (P5)."""
    return {
        "kind": op_kind,
        "status": "denied",
        "error": {
            "kind": "role_denied",
            "message": (
                f"op {op_kind!r} on task {task_id!r} requires the task's {role} "
                f"session; caller {caller!r} is not the {role}."
            ),
        },
    }


async def _authorize(op_kind: str, ctx: OpContext, backend, task_id: str, role: str):
    """Fetch the task + enforce role-based authority (P5): the caller's session_id
    must equal the task's ``role`` (``assignee`` or ``requester``). Both roles are
    immutable (set at create), so the read-then-check is race-free.

    Returns ``(task, None)`` when authorized, else ``(None, <result-dict>)`` (a
    not-found or a role-denied result for the handler to return verbatim)."""
    task = await backend.get(task_id)
    if task is None:
        return None, _not_found(op_kind, task_id)
    caller = _caller_session(ctx)
    if caller != getattr(task, role):
        return None, _role_denied(op_kind, task_id, role, caller)
    return task, None


async def _create(op, ctx: OpContext, caller) -> dict:
    # requester = the caller's session (the origin / assigner, §model "requester=self").
    # cross-session delegation supplies a different ``assignee``; a self-task defaults
    # the assignee to the caller. parent_id (optional, absorbs the old create_subtask)
    # must reference a task the CALLER owns as requester (tree decomposition, §12).
    requester = _caller_session(ctx)
    parent_id = getattr(op, "parent_id", None)
    if parent_id:
        parent = await _backend(ctx).get(parent_id)
        if parent is None:
            return _not_found("task.create", parent_id)
        if parent.requester != requester:
            return _role_denied("task.create", parent_id, "requester", requester)
    task = Task(
        task_id=uuid.uuid4().hex,
        name=op.name,
        assignee=(getattr(op, "assignee", None) or requester),
        requester=requester,
        origin=TaskOrigin(getattr(op, "origin", "self") or "self"),
        description=op.description,
        budget_cap=getattr(op, "budget_cap", None),
        parent_id=parent_id,
        created_by=_actor(ctx),
        deps=list(op.deps),
    )
    created = await _backend(ctx).create(task)
    _audit(ctx, "task.create", created.task_id, status=created.status.value,
           assignee=created.assignee, parent_id=parent_id)
    return _ok("task.create", task=created.to_dict())


async def _update_status(op, ctx: OpContext, caller) -> dict:
    # assignee-gated single-writer: the backend CAS-rejects when ctx.session_id
    # != the immutable assignee (#1814 routing-key). Atomic, so no separate check.
    task = await _backend(ctx).update_status(
        op.task_id, op.status, caller_session_id=_caller_session(ctx)
    )
    if task is None:
        return _not_found("task.update_status", op.task_id)
    _audit(ctx, "task.update_status", op.task_id, status=op.status)
    return _ok("task.update_status", task=task.to_dict())


async def _get(op, ctx: OpContext, caller) -> dict:
    # requester-gated: the requester polls its task's status (§model requester-IF).
    task, denied = await _authorize("task.get", ctx, _backend(ctx), op.task_id, "requester")
    if denied is not None:
        return denied
    return _ok("task.get", task=task.to_dict())


async def _list(op, ctx: OpContext, caller) -> dict:
    tasks = await _backend(ctx).list(
        assignee=op.assignee,
        requester=op.requester,
        status=op.status,
        parent_id=op.parent_id,
    )
    return _ok("task.list", tasks=[t.to_dict() for t in tasks])


async def _add_dependency(op, ctx: OpContext, caller) -> dict:
    # requester-gated: the decomposing requester owns the dependency topology (§13).
    _task, denied = await _authorize(
        "task.add_dependency", ctx, _backend(ctx), op.task_id, "requester")
    if denied is not None:
        return denied
    task = await _backend(ctx).add_dependency(op.task_id, op.depends_on)
    return _ok("task.add_dependency", task=task.to_dict())


async def _abort(op, ctx: OpContext, caller) -> dict:
    # requester-gated remove-op (§model abort=delete; full cancel/cascade in 2b).
    _task, denied = await _authorize("task.abort", ctx, _backend(ctx), op.task_id, "requester")
    if denied is not None:
        return denied
    task = await _backend(ctx).abort(op.task_id, reason=op.reason)
    _audit(ctx, "task.abort", op.task_id, status=task.status.value)
    return _ok("task.abort", task=task.to_dict())


async def _archive(op, ctx: OpContext, caller) -> dict:
    # requester-gated (transient — folded into abort in 2b).
    _task, denied = await _authorize("task.archive", ctx, _backend(ctx), op.task_id, "requester")
    if denied is not None:
        return denied
    task = await _backend(ctx).archive(op.task_id)
    _audit(ctx, "task.archive", op.task_id, status=task.status.value)
    return _ok("task.archive", task=task.to_dict())


async def _heartbeat(op, ctx: OpContext, caller) -> dict:
    # assignee-gated: only the worker session heartbeats its own task.
    task, denied = await _authorize("task.heartbeat", ctx, _backend(ctx), op.task_id, "assignee")
    if denied is not None:
        return denied
    # slice 7 adds predicate-eval + liveness-timeout; here it reports state.
    return _ok("task.heartbeat", task_id=op.task_id, state=task.status.value, unblocked=False)


async def _register_unblock_predicate(op, ctx: OpContext, caller) -> dict:
    # assignee-gated: the worker registers its own unblock predicate.
    _task, denied = await _authorize(
        "task.register_unblock_predicate", ctx, _backend(ctx), op.task_id, "assignee")
    if denied is not None:
        return denied
    await _backend(ctx).set_unblock_predicate(op.task_id, op.predicate)
    return _ok("task.register_unblock_predicate", task_id=op.task_id)


async def _comment(op, ctx: OpContext, caller) -> dict:
    comment_id = await _backend(ctx).add_comment(op.task_id, _actor(ctx) or "unknown", op.body)
    if comment_id is None:
        return _not_found("task.comment", op.task_id)
    return _ok("task.comment", task_id=op.task_id, comment_id=comment_id)


register("task.create", _create)
register("task.update_status", _update_status)
register("task.get", _get)
register("task.list", _list)
register("task.add_dependency", _add_dependency)
register("task.abort", _abort)
register("task.archive", _archive)
register("task.heartbeat", _heartbeat)
register("task.register_unblock_predicate", _register_unblock_predicate)
register("task.comment", _comment)
