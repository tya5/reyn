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

from reyn.core.offload.canonical import (
    CANONICAL_TODO,
    task_comment_to_canonical,
    task_heartbeat_to_canonical,
    task_op_to_canonical,
    task_register_unblock_predicate_to_canonical,
)
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
from reyn.tools.descriptions import task as _task_descriptions
from reyn.tools.types import ToolContext, ToolDefinition, ToolGates, ToolResult

# (op_kind, IROp model, LLM-facing description). op_kind == the IROp's ``kind``
# Literal; the exposed action name is the universal ``task__<verb>`` qualified
# form (set in universal_dispatch._OPERATION_RULES → this ToolDefinition).
#
# Descriptions relocated to reyn.tools.descriptions.task (Phase 3
# tool-description package refactor): each ``description`` string below now
# references a named ``ToolDescription.text`` there instead of an inline
# literal — the one data-tuple special case in the package (every other
# tool file uses a standalone ``_X_DESCRIPTION`` module constant instead).
# Byte-identical — no LLM-facing text change.
_TASK_OPS: tuple[tuple[str, type, str], ...] = (
    ("task.create", TaskCreateIROp, _task_descriptions.TASK_CREATE.text),
    ("task.update_status", TaskUpdateStatusIROp, _task_descriptions.TASK_UPDATE_STATUS.text),
    ("task.get", TaskGetIROp, _task_descriptions.TASK_GET.text),
    ("task.list", TaskListIROp, _task_descriptions.TASK_LIST.text),
    ("task.add_dependency", TaskAddDependencyIROp, _task_descriptions.TASK_ADD_DEPENDENCY.text),
    ("task.remove_dependency", TaskRemoveDependencyIROp, _task_descriptions.TASK_REMOVE_DEPENDENCY.text),
    ("task.repoint_dependency", TaskRepointDependencyIROp, _task_descriptions.TASK_REPOINT_DEPENDENCY.text),
    ("task.abort", TaskAbortIROp, _task_descriptions.TASK_ABORT.text),
    ("task.heartbeat", TaskHeartbeatIROp, _task_descriptions.TASK_HEARTBEAT.text),
    ("task.register_unblock_predicate", TaskRegisterUnblockPredicateIROp,
     _task_descriptions.TASK_REGISTER_UNBLOCK_PREDICATE.text),
    ("task.comment", TaskCommentIROp, _task_descriptions.TASK_COMMENT.text),
    ("task.assign", TaskAssignIROp, _task_descriptions.TASK_ASSIGN.text),
)


# Every task op's ToolDefinition MUST declare the SAME canonical mapper as its op_runtime
# registration (core/op_runtime/task.py) — both seams share the ONE ``"task.<verb>"`` source id, and
# ``declare_canonical`` rejects a conflicting re-declaration.
#   - #2681 Bucket C: the three status-shaped ops (heartbeat / register_unblock_predicate / comment)
#     get their own status-text mappers.
#   - #2681 Bucket B: the other 9 ops (create/update_status/get/list/add_dependency/
#     remove_dependency/repoint_dependency/abort/assign) return a full ``task=...to_dict()`` record —
#     genuinely structured, sharing the ONE ``task_op_to_canonical`` (record summary + attachment).
# All 12 are declared here; ``.get(op_kind, CANONICAL_TODO)`` below is a defensive fallback for a
# future 13th op added without a mapper (it would surface via the ratchet gate).
_TASK_OP_CANONICAL: "dict[str, Any]" = {
    "task.heartbeat": task_heartbeat_to_canonical,
    "task.register_unblock_predicate": task_register_unblock_predicate_to_canonical,
    "task.comment": task_comment_to_canonical,
    "task.create": task_op_to_canonical,
    "task.update_status": task_op_to_canonical,
    "task.get": task_op_to_canonical,
    "task.list": task_op_to_canonical,
    "task.add_dependency": task_op_to_canonical,
    "task.remove_dependency": task_op_to_canonical,
    "task.repoint_dependency": task_op_to_canonical,
    "task.abort": task_op_to_canonical,
    "task.assign": task_op_to_canonical,
}


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
            canonical=_TASK_OP_CANONICAL.get(op_kind, CANONICAL_TODO),
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
