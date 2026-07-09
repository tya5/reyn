"""Task-op ToolDefinitions (#1953 dynamic-wire item-1).

Exposes the 11 ``task.*`` control-IR ops as router/phase-callable
``invoke_action`` targets (``task__create``, ``task__update_status``, …) so a
chat agent can dynamically create + manage sub-tasks mid-flow (the
lightweight-task-ops model — no upfront orchestration tool).

Single-source factory: each ToolDefinition's ``parameters`` is derived from its
IROp model via ``model_json_schema()`` minus the ``kind`` discriminator (which
the factory sets, not the LLM) — so the exposed schema never drifts from the IR
model (e.g. the #2020 ``budget_cap`` removal auto-drops here, no stale field).

The handler bridges to ``op_runtime.execute_op`` via the router OpContext
factory, which carries the REAL chat-session id + Task backend (#1953
gate-threading) so the assignee/requester CAS gate enforces byte-equal to the
phase path. A task op is a WRITE through a gate keyed on ``OpContext.session_id``
(``_caller_session``); the bridge therefore REQUIRES a real-session OpContext and
raises rather than fall back to a session-less context that would mask the CAS
(no-bypass-by-construction).
"""
from __future__ import annotations

from typing import Any, Mapping

from reyn.core.offload.canonical import CANONICAL_TODO
from reyn.schemas.models import (
    TaskAbortIROp,
    TaskAddDependencyIROp,
    TaskAssignIROp,
    TaskCommentIROp,
    TaskCreateIROp,
    TaskGetIROp,
    TaskHeartbeatIROp,
    TaskListIROp,
    TaskRegisterUnblockPredicateIROp,
    TaskRemoveDependencyIROp,
    TaskRepointDependencyIROp,
    TaskUpdateStatusIROp,
)
from reyn.tools.types import ToolContext, ToolDefinition, ToolGates, ToolResult

# (op_kind, IROp model, LLM-facing description). op_kind == the IROp's ``kind``
# Literal; the exposed action name is the universal ``task__<verb>`` qualified
# form (set in universal_dispatch._OPERATION_RULES → this ToolDefinition).
_TASK_OPS: tuple[tuple[str, type, str], ...] = (
    ("task.create", TaskCreateIROp,
     "Create a task. While you are EXECUTING a task, a task you create is automatically "
     "owned by it (a sub-task) and — if you omit `assignee` — assigned to you to execute. "
     "For a TOP-LEVEL task: omitting `assignee` leaves it UNASSIGNED (it waits in the "
     "pending-assignment queue until a session claims it via task.assign); to execute it "
     "YOURSELF, set `assignee` to your own session; set it to another session to delegate. "
     "`deps` are depends-on task ids (born blocked until they complete). Use to decompose a "
     "complex request into trackable units."),
    ("task.update_status", TaskUpdateStatusIROp,
     "Declare a status transition on a task you are the ASSIGNEE of (the single "
     "writer). Terminal tasks reject writes."),
    ("task.get", TaskGetIROp, "Read one task record by id."),
    ("task.list", TaskListIROp,
     "List tasks, optionally narrowed by assignee / requester / status. Narrowing "
     "by `requester` (a task id) lists the sub-tasks that task owns."),
    ("task.add_dependency", TaskAddDependencyIROp,
     "Add a depends-on edge (you must be the requester/topology owner). "
     "Existence + cycle checked."),
    ("task.remove_dependency", TaskRemoveDependencyIROp,
     "Drop a depends-on edge (idempotent). Relaxing the graph may promote a "
     "now-ready dependent."),
    ("task.repoint_dependency", TaskRepointDependencyIROp,
     "Atomically repoint a dependency edge from one task to a substitute (the "
     "primary recovery move). The new edge is cycle-checked before any mutation."),
    ("task.abort", TaskAbortIROp,
     "Abort (delete) a task you requested + its sub-tree. Cooperative-terminal: "
     "the assignee's in-flight work is rejected at its next status-write."),
    ("task.heartbeat", TaskHeartbeatIROp,
     "Liveness ping for a blocked task; triggers unblock-predicate evaluation. "
     "Returns the current state."),
    ("task.register_unblock_predicate", TaskRegisterUnblockPredicateIROp,
     "Register a deterministic (code, no-LLM) unblock predicate evaluated at "
     "heartbeat; true → unblock."),
    ("task.comment", TaskCommentIROp,
     "Append a comment to a task's thread (durable inter-agent / human-in-the-loop "
     "protocol)."),
    ("task.assign", TaskAssignIROp,
     "Assign a session to a task. An UNASSIGNED task (in the pending-assignment queue) "
     "may be CLAIMED by anyone — set `assignee` to the session that will execute it. An "
     "already-assigned task may be reassigned ONLY by its current assignee (owner-initiated "
     "hand-off; others must request it via conversation). The new assignee is woken to "
     "execute it."),
)


def _parameters_for(model: type) -> dict[str, Any]:
    """JSON-schema ``parameters`` for a task IROp model, minus the ``kind``
    discriminator (set by the factory, never the LLM)."""
    schema = model.model_json_schema()
    props = dict(schema.get("properties", {}))
    props.pop("kind", None)
    required = [r for r in schema.get("required", []) if r != "kind"]
    out: dict[str, Any] = {"type": "object", "properties": props}
    if required:
        out["required"] = required
    return out


def _make_handler(op_kind: str, model: type):
    """Build the bridge handler for one task op: args → IROp → execute_op via a
    REAL-session OpContext (no None-placeholder mask)."""

    async def _handle(args: Mapping[str, Any], ctx: ToolContext) -> ToolResult:
        from reyn.core.op_runtime import execute_op
        from reyn.core.op_runtime.context import OpContext

        # Build the IROp; ``kind`` is fixed by the factory, the rest from args.
        try:
            op = model(kind=op_kind, **dict(args))
        except Exception as exc:  # pydantic validation → structured tool result
            return {
                "ok": False,
                "error_kind": "invalid_args",
                "error_message": f"{op_kind} args invalid: {exc}",
            }

        # Obtain a REAL-session OpContext via the host factory
        # (RouterHostAdapter.make_router_op_context → real session_id + backend).
        # A task op is a gated WRITE keyed on OpContext.session_id; we must NOT
        # fall back to a session-less context (it would mask-pass the CAS).
        factory = (
            ctx.router_state.op_context_factory
            if ctx.router_state is not None
            else None
        )
        op_ctx = factory() if factory is not None else None
        if not isinstance(op_ctx, OpContext):
            return {
                "ok": False,
                "error_kind": "no_session_context",
                "error_message": (
                    f"{op_kind} requires a session-scoped OpContext (the "
                    "assignee/requester gate is keyed on the caller session); "
                    "none was available on this call path."
                ),
            }

        return await execute_op(op, op_ctx)

    return _handle


def build_task_tool_definitions() -> list[ToolDefinition]:
    """The 12 task-op ToolDefinitions (router + phase callable)."""
    defs: list[ToolDefinition] = []
    for op_kind, model, description in _TASK_OPS:
        defs.append(ToolDefinition(
            canonical=CANONICAL_TODO,
            name=op_kind,  # e.g. "task.create" — registry-dispatch target name
            description=description,
            parameters=_parameters_for(model),
            gates=ToolGates(router="allow", phase="allow"),
            handler=_make_handler(op_kind, model),
            category="task",
            # Conservative: the write ops (create/update/abort/…) have side
            # effects; get/list are read-only but share the factory — side_effect
            # is the safe uniform classification (gating never under-restricts).
            purity="side_effect",
        ))
    return defs


TASK_TOOL_DEFINITIONS: list[ToolDefinition] = build_task_tool_definitions()
