"""Task op handlers (#1953) — route ``task.*`` Control IR ops to the Task backend.

The backend is resolved from ``OpContext.task_backend`` (the session-scoped,
config-selected backend) with an in-memory fallback for tests / direct
construction. Role-based authority (P5) gates each op on the caller's
``OpContext.session_id``: *assignee-gated* (``update_status`` / ``heartbeat`` /
``register_unblock_predicate``) vs *requester-gated* (``create`` /
``add_dependency`` / ``get`` / ``abort``). The single-writer is a fixed-equality
CAS ``assignee == caller session_id`` in the backend; ``abort`` is the
cooperative-terminal remove-op (archives the task + sub-tree; the assignee's
in-flight work is rejected by the terminal state at its next write — no forced
cancel). Each mutating handler emits a generic P6 audit event (``task_op``); the
backend's own ``task_events`` is the source of truth (the WAL closed vocab is not
expanded, P7).

Still deferred: abort UP-notify (2b-2), cycle-check (slice 6), predicate-eval
(slice 7).
"""
from __future__ import annotations

import uuid

from reyn.task import (
    InMemoryTaskBackend,
    Task,
    TaskCycleError,
    TaskDepNotFoundError,
    TaskOrigin,
    TaskState,
)
from reyn.task.model import TERMINAL_STATES

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


def resolve_task_backend(ctx: "OpContext | None"):
    """Public resolver (#1953 slice P3): the session-scoped backend on ``ctx`` when
    present, else the in-memory fallback. Lets non-op callers (the ``decompose``
    dispatch binding) reach the same backend the task ops use."""
    return _backend(ctx)


def _audit(ctx: OpContext, op_kind: str, task_id: str, **fields) -> None:
    """P6 audit emit for a task op (generic type; the backend's own task_events
    stays the source of truth — the WAL ``state_log`` closed vocab is NOT
    expanded, P7). No-op when the context has no event log (direct construction)."""
    events = getattr(ctx, "events", None)
    if events is not None:
        events.emit("task_op", op=op_kind, task_id=task_id, **fields)


def _ok(kind: str, **data) -> dict:
    return {"kind": kind, "status": "ok", **data}


def _fence_text(ctx: OpContext, text: "str | None") -> "str | None":
    """Fence a single free-text field with the Class-A structural fence when
    content-fencing is enabled (the global ``fence_enabled`` gate). Returns the
    text unchanged when fencing is off or the text is empty (the safety valve).
    Shared by ``_fence_view`` (the #2027 query path) and the WAKES execution path
    (the description delivered into a wake message). Reuses
    ``content_guard.fence_if_enabled`` (the same fence as the other content seams)."""
    from reyn.security.content_guard import fence_if_enabled
    if not text:
        return text
    return fence_if_enabled(text, getattr(ctx, "threat_scan", None))


def _fence_view(ctx: OpContext, task_view: dict) -> dict:
    """#2027: fence the cross-session-authorable free-text fields of a task VIEW
    (``description`` / ``name`` / ``result``) when content-fencing is enabled — so a
    delegated task's description (or a peer assignee's result) cannot inject the
    LLM via the read/list query path. Uniform: the view's text IS data, so no
    per-source trust classification (the gap is closed by always fencing); the
    structural fields (id / status / deps / dates) are OS-generated → not fenced.
    Mutates + returns the passed ``to_dict()`` copy (the stored Task is untouched)."""
    for field in ("description", "name", "result"):
        val = task_view.get(field)
        if isinstance(val, str) and val:
            task_view[field] = _fence_text(ctx, val)
    return task_view


def _not_found(kind: str, task_id: str) -> dict:
    return {"kind": kind, "status": "error", "error": f"task {task_id!r} not found"}


def _edge_error(op_kind: str, err: Exception) -> dict:
    """Decision-enabling error result for a rejected dependency edge (#1953 slice
    6, OQ-5): a structured ``status="error"`` dict (the edge + cycle path), NOT a
    raised exception through the op dispatcher."""
    if isinstance(err, TaskCycleError):
        return {
            "kind": op_kind,
            "status": "error",
            "error": {
                "kind": "cycle",
                "edge": [err.task_id, err.depends_on],
                "path": err.path,
            },
        }
    assert isinstance(err, TaskDepNotFoundError)
    return {
        "kind": op_kind,
        "status": "error",
        "error": {
            "kind": "dep_not_found",
            "edge": [err.task_id, err.depends_on],
        },
    }


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
        parent_id=parent_id,
        created_by=_actor(ctx),
        deps=list(op.deps),
    )
    try:
        created = await _backend(ctx).create(task)
    except (TaskCycleError, TaskDepNotFoundError) as err:
        # A born-with dependency is dangling or cycle-forming (OQ-1/OQ-4/OQ-5).
        return _edge_error("task.create", err)
    _audit(ctx, "task.create", created.task_id, status=created.status.value,
           assignee=created.assignee, parent_id=parent_id)
    # WAKES (item 5): a born-startable DELEGATED task → wake the assignee to
    # EXECUTE it now (the create-time counterpart of the dep-completion wake). A
    # born deps-less op-created task is PENDING (the default kept by the backend);
    # only a born-BLOCKED task (unmet deps) carries BLOCKED — so "born-ready" =
    # not-blocked = PENDING|READY. A self-task (assignee == requester) needs no
    # wake (the creator is the executor); a born-BLOCKED task is woken later when
    # its deps clear (recompute_readiness → wake_ready_dependent).
    waker = getattr(ctx, "task_waker", None)
    if (waker is not None
            and created.status in (TaskState.PENDING, TaskState.READY)
            and created.assignee != created.requester):
        await waker.wake_assigned(
            created, fenced_description=_fence_text(ctx, created.description))
    # #1800 slice 5c: task_start lifecycle hooks — the task has been created
    # (backend.create + the P6 audit). None dispatcher (direct/test construction
    # or no hooks) → no-op.
    hook_dispatcher = getattr(ctx, "hook_dispatcher", None)
    if hook_dispatcher is not None:
        await hook_dispatcher.dispatch(
            "task_start",
            {"point": "task_start", "task_id": created.task_id,
             "name": created.name, "assignee": created.assignee},
        )
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
    # OQ-3: a predecessor reaching `completed` drives DAG readiness (OS scheduling,
    # P3) — recompute its dependents and flip any fully-satisfied one blocked→ready
    # via the OS-authority backend method (no assignee CAS). `completed` only;
    # failed/aborted/archived deps don't satisfy an edge (H5/OQ-7 → slice 7).
    if task.status is TaskState.COMPLETED:
        promoted = await _backend(ctx).recompute_readiness(op.task_id)
        events = getattr(ctx, "events", None)
        waker = getattr(ctx, "task_waker", None)
        for p in promoted:
            if events is not None:
                # Generic P6 audit (like task_op/task_disposition); NOT a WAL
                # closed-vocab kind (WAL-vs-P6 separation, P7).
                events.emit("task_readiness", task_id=p.task_id, to="ready",
                            trigger=op.task_id)
            # slice 7: wake the now-ready dependent's session to continue its work
            # (the C3 re-invoke driver; None waker = no-op stub, slice 6-ext). WAKES
            # item 4: deliver the full description as fenced DATA (the trusted-OS
            # execute framing lives in the waker).
            if waker is not None:
                await waker.wake_ready_dependent(
                    p, fenced_description=_fence_text(ctx, p.description))
        # #1800 slice 5c: task_end lifecycle hooks — the task reached COMPLETED.
        # None dispatcher → no-op. (Aborted tasks terminate via the separate
        # _abort handler — see the task_end symmetry note there.)
        hook_dispatcher = getattr(ctx, "hook_dispatcher", None)
        if hook_dispatcher is not None:
            await hook_dispatcher.dispatch(
                "task_end",
                {"point": "task_end", "task_id": task.task_id, "status": "completed"},
            )
    elif task.status is TaskState.FAILED:
        # slice 6-ext §C: a non-completed terminal (the assignee declared `failed`)
        # doesn't satisfy a dependency edge → route the disposition to the task's
        # REQUESTER to decide recovery (§16; the OQ-7/H5 gap-close, #2107).
        await _route_terminal_to_requester(ctx, _backend(ctx), task, disposition="failed")
    return _ok("task.update_status", task=task.to_dict())


async def _get(op, ctx: OpContext, caller) -> dict:
    # requester-gated: the requester polls its task's status (§model requester-IF).
    task, denied = await _authorize("task.get", ctx, _backend(ctx), op.task_id, "requester")
    if denied is not None:
        return denied
    return _ok("task.get", task=_fence_view(ctx, task.to_dict()))


async def _list(op, ctx: OpContext, caller) -> dict:
    tasks = await _backend(ctx).list(
        assignee=op.assignee,
        requester=op.requester,
        status=op.status,
        parent_id=op.parent_id,
    )
    return _ok("task.list", tasks=[_fence_view(ctx, t.to_dict()) for t in tasks])


async def _add_dependency(op, ctx: OpContext, caller) -> dict:
    # requester-gated: the decomposing requester owns the dependency topology (§13).
    _task, denied = await _authorize(
        "task.add_dependency", ctx, _backend(ctx), op.task_id, "requester")
    if denied is not None:
        return denied
    try:
        task = await _backend(ctx).add_dependency(op.task_id, op.depends_on)
    except (TaskCycleError, TaskDepNotFoundError) as err:
        # OQ-1/OQ-4/OQ-5: dangling or cycle-forming edge → decision-enabling dict.
        return _edge_error("task.add_dependency", err)
    if task is None:
        return _not_found("task.add_dependency", op.task_id)
    _audit(ctx, "task.add_dependency", op.task_id, depends_on=op.depends_on)
    return _ok("task.add_dependency", task=task.to_dict())


async def _emit_readiness_if_changed(ctx: OpContext, task, before_status, trigger: str) -> None:
    """Emit the generic P6 ``task_readiness`` event when an OS re-derive changed a
    task's readiness (#1953 slice 6-ext). ``before_status`` is captured from the
    role-gate fetch (pre-mutation), so this fires only on an actual transition and
    only for the pre-run readiness states (ready / blocked). On a promote to
    ``ready`` (e.g. the parent repoints a dependent onto a completed substitute —
    the recovery loop), the task's session is also woken (slice 7)."""
    if task.status is before_status:
        return
    if task.status not in (TaskState.READY, TaskState.BLOCKED):
        return
    events = getattr(ctx, "events", None)
    if events is not None:
        events.emit("task_readiness", task_id=task.task_id,
                    to=task.status.value, trigger=trigger)
    if task.status is TaskState.READY:
        waker = getattr(ctx, "task_waker", None)
        if waker is not None:
            await waker.wake_ready_dependent(
                task, fenced_description=_fence_text(ctx, task.description))


async def _route_terminal_to_requester(
    ctx: OpContext, backend, terminal_task, *, disposition: str | None = None,
) -> None:
    """§16 (#2107): when a task reaches a non-completed terminal (aborted / failed /
    cap_exceeded) and has STILL-ALIVE dependents, notify its REQUESTER — the §16
    disposition notify-target (the request-owner) — to decide recovery. The requester
    re-wires via ordinary task ops (repoint to a substitute / remove the edge / fail
    them / handle the work itself) — P7, no `decision=` vocabulary.

    The requester is ALWAYS present (every task carries one), so a ROOT task is notified
    too — this restores §16 and fixes #2107. The prior parent-keyed routing returned
    early on ``if not parent_id`` (slice 6-ext §C), so a flat self-task plan's mid-task
    failure SILENTLY DROPPED the recovery wake and left its dependents stuck.

    Origin-gated to SELF (internal): a self-task's requester is a session → wake it. An
    EXTERNAL task's requester is an A2A/webhook stakeholder (not a session); its
    disposition rides the separate webhook channel (the abort-all-dependents + propagate
    is formalized in §16 S2), so this internal-recovery wake leaves it to the webhook —
    preserving the prior external behavior.

    ``disposition`` is the **first-class** terminal reason carried in BOTH the P6 event
    and the requester payload (#1953 slice 8 no-conflation): a budget ``cap_exceeded``
    must stay distinguishable from a genuine ``failed`` — the recovery differs. Defaults
    to the task's status value (the abort path) when not given explicitly. The wake is via
    ``OpContext.task_waker`` (None = no-op stub — only the P6 audit fires)."""
    if terminal_task.origin is not TaskOrigin.SELF:
        return  # external → the webhook channel (§16 S2), not an internal session wake
    disp = disposition or terminal_task.status.value
    deps_on_it = await backend.dependents(terminal_task.task_id)
    stuck = [d for d in deps_on_it if d.status not in TERMINAL_STATES]
    if not stuck:
        return
    requester = terminal_task.requester
    events = getattr(ctx, "events", None)
    if events is not None:
        # Generic P6 (P7); NOT a WAL closed-vocab kind (WAL-vs-P6 separation).
        events.emit(
            "task_dependency_aborted", task_id=terminal_task.task_id,
            disposition=disp, requester=requester,
            dependents=[d.task_id for d in stuck],
        )
    waker = getattr(ctx, "task_waker", None)
    if waker is not None:
        await waker.notify_requester_decide(
            requester_session=requester, terminal_task=terminal_task,
            dependents=stuck, disposition=disp,
        )


async def _remove_dependency(op, ctx: OpContext, caller) -> dict:
    # requester-gated: the decomposing requester owns the dependency topology (§13).
    _task, denied = await _authorize(
        "task.remove_dependency", ctx, _backend(ctx), op.task_id, "requester")
    if denied is not None:
        return denied
    before = _task.status  # captured pre-mutation (InMemory mutates in place)
    task = await _backend(ctx).remove_dependency(op.task_id, op.depends_on)
    if task is None:
        return _not_found("task.remove_dependency", op.task_id)
    _audit(ctx, "task.remove_dependency", op.task_id, depends_on=op.depends_on)
    await _emit_readiness_if_changed(ctx, task, before, op.task_id)
    return _ok("task.remove_dependency", task=task.to_dict())


async def _repoint_dependency(op, ctx: OpContext, caller) -> dict:
    # requester-gated: the parent re-wires the topology (its recovery move, §C).
    _task, denied = await _authorize(
        "task.repoint_dependency", ctx, _backend(ctx), op.task_id, "requester")
    if denied is not None:
        return denied
    before = _task.status
    try:
        task = await _backend(ctx).repoint_dependency(
            op.task_id, op.from_depends_on, op.to_depends_on)
    except (TaskCycleError, TaskDepNotFoundError) as err:
        # The NEW edge is dangling or cycle-forming → nothing changed (atomic).
        return _edge_error("task.repoint_dependency", err)
    if task is None:
        return _not_found("task.repoint_dependency", op.task_id)
    _audit(ctx, "task.repoint_dependency", op.task_id,
           from_depends_on=op.from_depends_on, to_depends_on=op.to_depends_on)
    await _emit_readiness_if_changed(ctx, task, before, op.task_id)
    return _ok("task.repoint_dependency", task=task.to_dict())


async def _abort(op, ctx: OpContext, caller) -> dict:
    # requester-gated remove-op (§model abort=delete). Cooperative-terminal
    # (Option B): the backend archives the task + its sub-tree (DOWN-cascade);
    # the assignee's in-flight work is rejected by the terminal state at its next
    # status-write (no forced cancel, no sibling-kill). task.archive is folded in.
    _task, denied = await _authorize("task.abort", ctx, _backend(ctx), op.task_id, "requester")
    if denied is not None:
        return denied
    aborted = await _backend(ctx).abort(op.task_id, reason=op.reason)
    if not aborted:
        return _not_found("task.abort", op.task_id)
    # UP-notify (2b-2): emit a generic, term-neutral P6 disposition event per
    # aborted task carrying requester + origin. The A2A layer (slice 5) consumes
    # these and fires the external (webhook) channel for origin=external tasks
    # (the persistent stakeholders). §16 (#2107): internal requesters ARE notified
    # too — via _route_terminal_to_requester below (the requester's session is woken
    # to recover stuck dependents). The prior "internal requesters need no notify"
    # claim left a flat self-task plan's mid-task failure silently stuck.
    events = getattr(ctx, "events", None)
    if events is not None:
        for t in aborted:
            events.emit(
                "task_disposition", task_id=t.task_id, disposition="aborted",
                requester=t.requester, origin=t.origin.value, root=op.task_id,
            )
    root = aborted[0]
    # §16 (#2107): route the aborted root to its REQUESTER so a stuck sibling dependent
    # gets a recovery decision (the OQ-7/H5 gap-close). A root self-task is now notified
    # (previously dropped on the missing parent_id). The cascade's descendants are
    # themselves terminal → _route_terminal_to_requester finds no stuck dependents for them.
    await _route_terminal_to_requester(ctx, _backend(ctx), root, disposition="aborted")
    # #1800 slice 5c: task_end lifecycle hooks for the aborted sub-tree — SYMMETRIC
    # with task_start (at create) + task_end (at COMPLETED): every started task
    # fires an end. ``status="aborted"`` lets an operator discriminate completion
    # from abort. Mirrors the task_disposition loop above (one per aborted task).
    # None dispatcher → no-op.
    hook_dispatcher = getattr(ctx, "hook_dispatcher", None)
    if hook_dispatcher is not None:
        for t in aborted:
            await hook_dispatcher.dispatch(
                "task_end",
                {"point": "task_end", "task_id": t.task_id, "status": "aborted"},
            )
    return _ok("task.abort", task=root.to_dict())


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
register("task.remove_dependency", _remove_dependency)
register("task.repoint_dependency", _repoint_dependency)
register("task.abort", _abort)
register("task.heartbeat", _heartbeat)
register("task.register_unblock_predicate", _register_unblock_predicate)
register("task.comment", _comment)
