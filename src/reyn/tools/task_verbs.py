"""describe_task / list_tasks / cancel_task — proposal 0067 P4 (#3978).

Read/act against the CALLING session's own ``ChainManager`` (the
settle-path handle substrate — ``reyn.runtime.services.chain_manager``),
threaded via ``RouterCallerState.chains`` (mirrors ``pipeline_registry``'s
own threading, ``RouterHostAdapter.get_chains()``).

Per ADR-0040 D4 ("push-at-settle with immediate deletion") a settled
task's handle is gone — so all three ops answer only about RUNNING tasks;
there is no ``read_task_result``. A handle with ``kind=None`` (a legacy
delegate-relay chain, not yet a typed task — proposal 0067 P6 assigns one
when ``delegate_to_agent``'s completion folds into this substrate) is
excluded from every read here: it is not describable as a task today.

``cancel_task``'s one hard requirement (architect, #3978): a handle whose
``cancel`` hook is ``None`` (a crash-recovered chain — the live callable
belonged to the dead process) MUST NOT be reported as a success. Reporting
"cancelled" while the task keeps running is the same silent lie as
displaying an unenforced ``ttl_seconds`` would be.
"""
from __future__ import annotations

from typing import Any, Mapping

from reyn.core.offload.canonical import (
    cancel_task_to_canonical,
    describe_task_to_canonical,
    list_tasks_to_canonical,
)
from reyn.tools.types import ToolContext, ToolDefinition, ToolGates, ToolResult


def _chains(ctx: ToolContext) -> Any:
    rs = getattr(ctx, "router_state", None)
    return getattr(rs, "chains", None) if rs is not None else None


def _describe(chain: Any) -> dict[str, Any]:
    requester = chain.requester
    return {
        "task_id": chain.chain_id,
        "kind": chain.kind,
        "status": chain.status.value,
        "session": chain.origin_sid or "main",
        "requester": {
            "agent_name": requester.agent_name,
            "session_id": requester.session_id,
        },
    }


# ── describe_task ────────────────────────────────────────────────────────────

_DESCRIBE_TASK_DESCRIPTION = (
    "Describe one currently RUNNING task by its task_id (the handle a "
    "run_prompt/run_pipeline async launch returned). A settled task's "
    "handle no longer exists — use this for progress checking or anomaly "
    "detection, not to retrieve a finished result (results arrive via a "
    "task_settled push, not a poll)."
)

_DESCRIBE_TASK_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "task_id": {
            "type": "string",
            "description": "The task's handle, as returned by its async launch.",
        },
    },
    "required": ["task_id"],
}


async def _handle_describe_task(args: Mapping[str, Any], ctx: ToolContext) -> ToolResult:
    chains = _chains(ctx)
    task_id = args["task_id"]
    chain = chains.get(task_id) if chains is not None else None
    if chain is None or chain.kind is None:
        return {"ok": False, "error": f"no running task {task_id!r}"}
    return {"ok": True, **_describe(chain)}


DESCRIBE_TASK = ToolDefinition(
    canonical=describe_task_to_canonical,
    name="describe_task",
    router_dispatched=True,
    description=_DESCRIBE_TASK_DESCRIPTION,
    parameters=_DESCRIBE_TASK_PARAMETERS,
    gates=ToolGates(router="allow"),
    handler=_handle_describe_task,
    category="discovery",
    purity="read_only",
)


# ── list_tasks ───────────────────────────────────────────────────────────────

_LIST_TASKS_DESCRIPTION = (
    "List currently RUNNING tasks (async run_prompt/run_pipeline launches "
    "not yet settled), optionally filtered by kind. Settled tasks are not "
    "listed — their handle no longer exists (ADR-0040 D4)."
)

_LIST_TASKS_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "kind": {
            "type": "string",
            "description": (
                "Optional — restrict to one task kind (e.g. 'prompt', "
                "'pipeline'). Omit to list every running task."
            ),
        },
    },
}


async def _handle_list_tasks(args: Mapping[str, Any], ctx: ToolContext) -> ToolResult:
    chains = _chains(ctx)
    if chains is None:
        return {"tasks": []}
    kind_filter = args.get("kind")
    tasks = []
    for chain_id in chains.all_chain_ids():
        chain = chains.get(chain_id)
        if chain is None or chain.kind is None:
            continue
        if kind_filter is not None and chain.kind != kind_filter:
            continue
        described = _describe(chain)
        del described["requester"]  # list view: {task_id, kind, status, session} only
        tasks.append(described)
    return {"tasks": tasks}


LIST_TASKS = ToolDefinition(
    canonical=list_tasks_to_canonical,
    name="list_tasks",
    router_dispatched=True,
    description=_LIST_TASKS_DESCRIPTION,
    parameters=_LIST_TASKS_PARAMETERS,
    gates=ToolGates(router="allow"),
    handler=_handle_list_tasks,
    category="discovery",
    purity="read_only",
)


# ── cancel_task ──────────────────────────────────────────────────────────────

_CANCEL_TASK_DESCRIPTION = (
    "Request cooperative cancellation of a currently RUNNING task by its "
    "task_id. Returns immediately with status='cancel_requested' — the "
    "task stops at its next safe boundary, and its settle (task_settled) "
    "still fires with status='cancelled' once it does. A task recovered "
    "after a crash cannot be cancelled (no live process to signal); this "
    "returns an explicit error, never a false 'cancelled'."
)

_CANCEL_TASK_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "task_id": {
            "type": "string",
            "description": "The task's handle, as returned by its async launch.",
        },
    },
    "required": ["task_id"],
}


async def _handle_cancel_task(args: Mapping[str, Any], ctx: ToolContext) -> ToolResult:
    chains = _chains(ctx)
    task_id = args["task_id"]
    chain = chains.get(task_id) if chains is not None else None
    if chain is None or chain.kind is None:
        return {"task_id": task_id, "status": "error", "error": f"no running task {task_id!r}"}
    if chain.cancel is None:
        # Architect's witness requirement (#3978): a recovered handle's
        # cancel hook belonged to the dead process — reporting success
        # here would be the exact silent lie an unenforced ttl_seconds is.
        return {
            "task_id": task_id, "status": "error",
            "error": (
                f"task {task_id!r} cannot be cancelled — no live process is "
                "reachable (likely a handle recovered after a crash)"
            ),
        }
    chain.cancel()
    return {"task_id": task_id, "status": "cancel_requested"}


CANCEL_TASK = ToolDefinition(
    canonical=cancel_task_to_canonical,
    name="cancel_task",
    router_dispatched=True,
    description=_CANCEL_TASK_DESCRIPTION,
    parameters=_CANCEL_TASK_PARAMETERS,
    gates=ToolGates(router="allow"),
    handler=_handle_cancel_task,
    category="io",
    purity="side_effect",
)
