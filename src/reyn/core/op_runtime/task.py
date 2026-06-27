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
    TaskLinkType,
    TaskOrigin,
    TaskRequesterKind,
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


def _reject_unknown_assignee(ctx: OpContext, assignee, caller_session) -> "dict | None":
    """#2187 dogfood-fix (#45): a decision-enabling error result when a DELEGATED
    assignee (≠ the caller) does not resolve to a live session of the agent — delegating
    to a non-existent (agent, session) would silently orphan the task (its execute-wake
    is dropped). None = ok / self-task (assignee == caller, the live caller) / no waker
    wired (direct construction / tests — the opt-in contract)."""
    waker = getattr(ctx, "task_waker", None)
    if waker is None or assignee == caller_session:
        return None
    if not waker.resolves(assignee):
        return {
            "kind": "task.create", "status": "error",
            "error": {
                "kind": "unknown_assignee",
                "message": (
                    f"task.create rejected: assignee {assignee!r} is not a live session "
                    f"of agent {getattr(ctx, 'agent_id', None)!r} — cannot delegate to a "
                    f"non-existent (agent, session); the task would be orphaned."
                ),
            },
        }
    return None


async def _record_subscribed(ctx: OpContext, created) -> None:
    """#2187 backend-master: append the ``task_subscribed`` binding (the Reyn-internal
    task↔session subscription) to the WAL. No-op when the OpContext carries no
    subscription writer (direct construction / tests / no state_log) — the opt-in
    contract, same as ``task_waker``."""
    writer = getattr(ctx, "task_subscription_writer", None)
    if writer is not None:
        await writer.record_subscribed(
            created.task_id, assignee=created.assignee, requester=created.requester,
            requester_kind=created.requester_kind.value)


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


def _open_children_error(task_id: str, counts) -> dict:
    """Decision-enabling error (#2187 §3.4, 5c completion-join): a task may not reach
    DONE while it still owns open (non-terminal) children — the whole decomposition
    tree must complete first. NOT a raised exception: the LLM gets the structured
    error and decides (wait — it is re-woken by the child_settled reconcile when its
    children settle — or abort the children), exactly like ``_reject_unknown_assignee``."""
    return {
        "kind": "task.update_status",
        "status": "error",
        "error": {
            "kind": "open_children",
            "message": (
                f"Cannot complete task {task_id!r}: {counts.awaited} awaited "
                f"+ {counts.background} background child task(s) are still open. Wait "
                f"for them to finish (you will be re-woken when they settle), or abort "
                f"them — then complete."
            ),
            "awaited": counts.awaited,
            "background": counts.background,
        },
    }


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
    # §16 recursive-request: requester + requester_kind are OS-SET from the caller's
    # EXECUTION context — NEVER an op field, so the LLM cannot mark ownership to
    # mis-route a later recovery (the §16 security invariant). When the caller is
    # executing a task-as-request (``ctx.current_task_id`` set by the OS for that
    # turn), the new sub-task is OWNED by that task (``requester`` = the task id,
    # ``requester_kind`` = task). Otherwise it is a top-level / session-owned task
    # (``requester`` = the caller session, kind = session — the original model).
    # cross-session delegation supplies a different ``assignee``; a self-task defaults
    # the assignee to the caller SESSION (the executor — NOT the requester, which is
    # now a task id in the recursive case; a task-id assignee would break the
    # single-writer CAS ``assignee == caller_session_id``). The requester edge IS
    # the ownership/decomposition relation now — the legacy parent_id tree was
    # removed (§16 slice C), so there is no op-supplied parent + no ownership-check
    # (ownership is OS-derived from the execution context, not an op field).
    caller_session = _caller_session(ctx)
    current_task_id = getattr(ctx, "current_task_id", None)
    if current_task_id:
        requester = current_task_id
        requester_kind = TaskRequesterKind.TASK
    else:
        requester = caller_session
        requester_kind = TaskRequesterKind.SESSION
    task = Task(
        task_id=uuid.uuid4().hex,
        name=op.name,
        assignee=(getattr(op, "assignee", None) or caller_session),
        requester=requester,
        requester_kind=requester_kind,
        # #2187 §3.5 (5b): the decomposition-link type (awaited gates the parent's
        # completion; background runs parallel). Marked at create, durable. Default
        # AWAITED (the safe, blocking default); consulted only for a sub-task
        # (requester_kind=TASK), unused on a top-level task.
        link_type=TaskLinkType(getattr(op, "link_type", None) or "awaited"),
        origin=TaskOrigin(getattr(op, "origin", "self") or "self"),
        description=op.description,
        created_by=_actor(ctx),
        deps=list(op.deps),
    )
    # #2187 dogfood-fix (#45): reject a delegation to a NON-EXISTENT (agent, session)
    # BEFORE creating anything — a bare-sid assignee that names no live session would
    # have its execute-wake silently dropped (the orphan root cause). Self-task /
    # no-waker → no check (opt-in).
    denied = _reject_unknown_assignee(ctx, task.assignee, caller_session)
    if denied is not None:
        return denied
    try:
        created = await _backend(ctx).create(task)
    except (TaskCycleError, TaskDepNotFoundError) as err:
        # A born-with dependency is dangling or cycle-forming (OQ-1/OQ-4/OQ-5).
        return _edge_error("task.create", err)
    # #2187 backend-master: append the task↔session BINDING to the WAL (the Reyn-internal
    # SUBSCRIPTION — the assignee that executes it + the requester parent that owns it).
    # The backend holds task-STATE (the external master); this binding is what Reyn owns +
    # rewinds, so it lives in the WAL.
    await _record_subscribed(ctx, created)
    _audit(ctx, "task.create", created.task_id, status=created.status.value,
           assignee=created.assignee)
    # WAKES (item 5): a born-startable DELEGATED task → wake the assignee to
    # EXECUTE it now (the create-time counterpart of the dep-completion wake). A
    # born deps-less op-created task is PENDING (the default kept by the backend);
    # only a born-BLOCKED task (unmet deps) carries BLOCKED — so "born-ready" =
    # not-blocked = READY. A self-task (assignee == requester) needs no
    # wake (the creator is the executor); a born-BLOCKED task is woken later when
    # its deps clear (recompute_readiness → wake_ready_dependent).
    waker = getattr(ctx, "task_waker", None)
    if (waker is not None
            and created.status is TaskState.READY
            and created.assignee != created.requester):
        # #2187 Stage 4: publish the state-change through the single pub/sub seam
        # (event_type "assigned"; delivered to the assignee subscriber to execute).
        await waker.publish_task_event(
            "assigned", created, fenced_description=_fence_text(ctx, created.description))
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
    # #2187 backend-master: the single-writer CAS is OP-LAYER ownership-gating — the
    # caller must be the task's CURRENT assignee (the WAL-subscription binding, hydrated
    # onto the fetched task; mutable, so this is the current owner not a frozen one).
    # _authorize does exactly that assignee-check. Read-then-request is safe under the
    # single-writer invariant (one writer per task) + WAL ordering. The backend (the
    # task-state MASTER) then APPLIES the request — its only gate is the terminal-check
    # (a state-validity rule: no transition out of a terminal state), NOT the binding.
    _bound, denied = await _authorize(
        "task.update_status", ctx, _backend(ctx), op.task_id, "assignee")
    if denied is not None:
        return denied
    # #2187 §3.4 (5c) completion-join: RUNNING → DONE only when the decomposition tree
    # is complete (no open child of EITHER link type — awaited gates execution, but the
    # final DONE requires the whole tree terminal). Attempting DONE with open children
    # returns a DECISION-ENABLING error (the parent waits — it is re-woken by the
    # child_settled reconcile when its children settle — or aborts them); the LLM is not
    # stopped. The check is the task's OWN children, before the backend applies DONE.
    if op.status == TaskState.DONE.value:
        counts = await _backend(ctx).open_child_counts(op.task_id)
        if counts.awaited + counts.background > 0:
            return _open_children_error(op.task_id, counts)
    task = await _backend(ctx).update_status(op.task_id, op.status)
    if task is None:
        return _not_found("task.update_status", op.task_id)
    _audit(ctx, "task.update_status", op.task_id, status=op.status)
    # OQ-3: a predecessor reaching `done` drives DAG readiness (OS scheduling,
    # P3) — recompute its dependents and flip any fully-satisfied one blocked→ready
    # via the OS-authority backend method (no assignee CAS). `done` only;
    # failed/aborted deps don't satisfy an edge (H5/OQ-7 → slice 7).
    if task.status is TaskState.DONE:
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
                await waker.publish_task_event(  # #2187 Stage 4: pub/sub seam ("ready")
                    "ready", p, fenced_description=_fence_text(ctx, p.description))
        # #1800 slice 5c: task_end lifecycle hooks — the task reached DONE.
        # None dispatcher → no-op. (Aborted tasks terminate via the separate
        # _abort handler — see the task_end symmetry note there.)
        hook_dispatcher = getattr(ctx, "hook_dispatcher", None)
        if hook_dispatcher is not None:
            await hook_dispatcher.dispatch(
                "task_end",
                {"point": "task_end", "task_id": task.task_id, "status": "done"},
            )
        # #2187 §3.5 (5c): a DONE *decomposition child* (requester_kind=TASK) reconciles
        # its PARENT (the child_settled waker — the parent's awaited/total counts may have
        # crossed 0). A top-level DONE (requester_kind=SESSION) has no decomposition parent
        # and must NOT route to the requester (DONE is not a recovery trigger — only the
        # DAG promotion above applies, as before 5c).
        if task.requester_kind is TaskRequesterKind.TASK:
            await _settle_terminal(ctx, _backend(ctx), task, disposition="done")
    elif task.status is TaskState.FAILED:
        # slice 6-ext §C: a non-completed terminal (the assignee declared `failed`)
        # doesn't satisfy a dependency edge → route the disposition to the task's
        # OWNER to decide recovery (§16; the OQ-7/H5 gap-close, #2107). #2187 5c: the
        # requester_kind-exclusive routing (TASK→child_settled, SESSION→requester).
        await _settle_terminal(ctx, _backend(ctx), task, disposition="failed")
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
            await waker.publish_task_event(  # #2187 Stage 4: pub/sub seam ("ready")
                "ready", task, fenced_description=_fence_text(ctx, task.description))


async def _settle_terminal(ctx: OpContext, backend, task, *, disposition: str) -> None:
    """Route a task's terminal transition to its OWNER, EXCLUSIVELY by requester_kind
    (#2187 §3.5, 5c) — so no owner is double-woken by construction:

    - ``requester_kind=TASK`` (a decomposition child): wake the PARENT's managing
      session via the ``child_settled`` reconcile-wake. ONE wake subsumes both
      dependent-recovery (it carries the disposition + the child's stuck dependents)
      AND completion-driving (the parent's open-child counts). Mutually-exclusive
      firing (decision 3 — ``total`` subsumes ``awaited``): ``total==0`` → the parent
      may complete; ``elif awaited==0`` → the parent is unblocked, continue; ``elif``
      the child failed/aborted → recover its stuck dependents; ``else`` the parent is
      still blocked on awaited children with no failure → no wake (§3.5: it idles).
      A phantom parent (binding present, backend row absent) → audit + drop the wake
      (the 5d reconciliation domain). A ``requester_kind=TASK`` task is always
      SELF-origin (a sub-task is internal), so there is no EXTERNAL-webhook path here.
    - ``requester_kind=SESSION`` (a top-level request): the existing requester
      recovery (``_route_terminal_to_requester`` — unchanged, incl. EXTERNAL)."""
    if task.requester_kind is not TaskRequesterKind.TASK:
        await _route_terminal_to_requester(ctx, backend, task, disposition=disposition)
        return
    events = getattr(ctx, "events", None)
    parent = await backend.get(task.requester)
    if parent is None:
        # phantom parent — no managing session to wake (the 5d reconciliation domain).
        if events is not None:
            events.emit("task_child_settled", task_id=task.task_id,
                        parent=task.requester, disposition=disposition, reason="phantom")
        return
    counts = await backend.open_child_counts(parent.task_id)
    failed = disposition != TaskState.DONE.value
    if counts.awaited + counts.background == 0:
        reason = "final_completion"
    elif counts.awaited == 0:
        reason = "continue"
    elif failed:
        reason = "recovery"
    else:
        reason = None  # still blocked on awaited children, no failure → the parent idles
    if events is not None:
        # Generic P6 audit (P7; NOT a WAL closed-vocab kind).
        events.emit("task_child_settled", task_id=task.task_id, parent=parent.task_id,
                    disposition=disposition, reason=reason or "idle",
                    awaited=counts.awaited, background=counts.background)
    if reason is None:
        return
    stuck = [d.task_id for d in await backend.dependents(task.task_id)
             if d.status not in TERMINAL_STATES]
    waker = getattr(ctx, "task_waker", None)
    if waker is not None:
        await waker.publish_task_event(
            "child_settled", parent, child_task=task, disposition=disposition,
            reason=reason, awaited=counts.awaited, background=counts.background,
            stuck_dependents=stuck)


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
        # §16 S2 (origin-split): an EXTERNAL terminal gets no in-session recovery wake (no
        # session to wake — the requester is an A2A/webhook stakeholder). Its stuck dep-DAG
        # dependents can't be recovered, so abort them → the webhook sweep propagates each
        # archived dependent to the A2A client. backend.abort's own EXTERNAL cascade then
        # handles the transitive descendants. This covers the failed / cap_exceeded
        # triggers (which mark X terminal WITHOUT calling backend.abort on X); the abort /
        # cancel / kill triggers already feed backend.abort directly, so by the time we
        # reach here on the abort path X's dependents are already archived → no-op
        # (idempotent via the terminal-state filter).
        for dep in await backend.dependents(terminal_task.task_id):
            if dep.status not in TERMINAL_STATES:
                await backend.abort(dep.task_id)
        return
    disp = disposition or terminal_task.status.value
    deps_on_it = await backend.dependents(terminal_task.task_id)
    stuck = [d for d in deps_on_it if d.status not in TERMINAL_STATES]
    if not stuck:
        return
    requester = terminal_task.requester
    # §16 recursive-request: resolve the notify-TARGET session. A ``session``
    # requester IS the target (the original S1 path). A ``task`` requester (a
    # task-as-request owns this dependent's failed task) resolves to that task's
    # ASSIGNEE — the managing session that owns + executes the request (parent ==
    # requester by construction → no drift). One hop suffices: an assignee is always
    # a session, never another task. This is the recursive generalization of S1.
    requester_session = requester
    # §16 B1 (recursive-request): when the requester is a TASK, ``requester`` IS the
    # managing task-as-request's id (T). Carry it to the wake so the managing session's
    # recovery turn is stamped with current_task=T → a replacement it creates is OWNED
    # by T (closes hole (i) recovery-create). None for a session-requester (a top-level
    # request's recovery stays session-owned). Interleaving-precise: the recovery wake
    # is task-specific (for T), so stamping current=T cannot leak into another task's turn.
    managing_task_id = None
    if terminal_task.requester_kind is TaskRequesterKind.TASK:
        owner = await backend.get(requester)
        requester_session = owner.assignee if owner is not None else None
        managing_task_id = requester
    events = getattr(ctx, "events", None)
    if events is not None:
        # Generic P6 (P7); NOT a WAL closed-vocab kind (WAL-vs-P6 separation). The
        # event records the TRUE owner (``requester`` — a session or a task id) for
        # audit; the wake goes to the resolved managing session below.
        events.emit(
            "task_dependency_aborted", task_id=terminal_task.task_id,
            disposition=disp, requester=requester,
            dependents=[d.task_id for d in stuck],
        )
    if requester_session is None:
        # The owning task-as-request is gone → no managing session to wake. The
        # ownership-cascade (slice B) aborts such orphaned dependents; here the audit
        # has fired, so drop the wake (cf. S1's bare-no-live-session drop).
        return
    waker = getattr(ctx, "task_waker", None)
    if waker is not None:
        # #2187 Stage 4: publish the terminal state-change through the single pub/sub
        # seam (delivered to the requester subscriber to decide recovery).
        await waker.publish_task_event(
            "terminal", terminal_task,
            requester_session=requester_session,
            dependents=stuck, disposition=disp, managing_task_id=managing_task_id,
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
    # §16 (#2107): route the aborted root to its OWNER so a stuck sibling dependent gets
    # a recovery decision (the OQ-7/H5 gap-close). A root self-task is now notified
    # (previously dropped on the missing parent_id). The cascade's descendants are
    # themselves terminal → no stuck dependents for them. #2187 5c: requester_kind-
    # exclusive routing (TASK→child_settled reconcile, SESSION→requester recovery).
    await _settle_terminal(ctx, _backend(ctx), root, disposition="aborted")
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
