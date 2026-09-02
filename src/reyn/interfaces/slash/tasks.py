"""``/tasks`` — list and cancel this session's own currently RUNNING tasks
(operator-facing wrapper over the LLM ops, #5654).

``/tasks``               → list running tasks (prompt/pipeline async launches).
``/tasks cancel <id>``   → request cooperative cancellation of one.

Thin adapter over the SAME typed ops the LLM ``list_tasks``/``cancel_task``
tool calls use (mirrors ``/plugin``'s own pattern, ``slash/plugin.py``) —
never re-implements list/cancel against ``ChainManager`` directly, which
would create a second place the "which tasks are visible, what does cancel
actually do" contract could drift from the LLM-facing one.

Unlike ``/plugin``, this goes through :func:`dispatch_tool`
(``core/dispatch/dispatcher.py``), not the bare ``invoke_tool`` ``/plugin``
calls — ``dispatch_tool`` is what emits the ``tool_called``/``tool_returned``
audit trail (``caller_kind="operator"``, #5654's own addition to that field's
vocabulary), and an operator's own cancel is exactly the action this repo's
audit band exists to make reconstructable ("who cancelled this task" must be
answerable from the event log, not just inferred from the WAL).

Scope (owner decision, 2026-09-02): this session's own ``ChainManager`` only
— no ``--all`` / cross-session listing. What ``list_tasks`` sees is what
``/tasks`` shows.

Cancellation reach (#3978, unchanged by this file): a ``prompt`` task's
cancel targets the OTHER session's current turn, not this chain specifically
— if that session has since moved on to different work, THAT is what stops.
There is also no record here of WHICH session a prompt task's target
resolved to (only the target AGENT's name is persisted, see
``chain_manager.py``'s own field list) — the confirmation wording below says
only as much as the substrate actually knows.
"""
from __future__ import annotations

from typing import Any

from reyn.interfaces.slash import SlashContext, reply, reply_error, slash

_USAGE = "usage: /tasks | /tasks cancel <task_id> [confirm]"


async def _build_tasks_tool_context(ctx: "SlashContext") -> Any:
    from reyn.tools.types import ToolContext, build_resource_caller_state

    host = ctx.session.router_host
    router_state = await build_resource_caller_state(host)
    return ToolContext(
        events=host.events,
        permission_resolver=getattr(host, "permission_resolver", None),
        workspace=getattr(host, "workspace", None),
        caller_kind="operator",
        router_state=router_state,
        resolver=getattr(host, "resolver", None),
        hot_reloader=getattr(host, "hot_reloader", None),
        state_log=getattr(host, "state_log", None),
        agent_name=getattr(host, "agent_name", None),
    )


async def _dispatch(name: str, args: dict, ctx: "SlashContext") -> dict:
    """Route one op call through :func:`dispatch_tool`, for the audit trail
    (``tool_called``/``tool_returned``, ``caller_kind="operator"``) — the
    one thing calling the handler directly (or via the bare ``invoke_tool``
    ``/plugin`` uses) would silently skip."""
    from reyn.core.dispatch.dispatcher import DispatchContext, dispatch_tool
    from reyn.tools import get_default_registry
    from reyn.tools.dispatch import invoke_tool
    from reyn.tools.task_verbs import CANCEL_TASK, LIST_TASKS

    tool_ctx = await _build_tasks_tool_context(ctx)
    definition = {"list_tasks": LIST_TASKS, "cancel_task": CANCEL_TASK}[name]
    dispatch_ctx = DispatchContext(
        caller_kind="operator",
        caller_id=getattr(tool_ctx, "agent_name", None) or "",
        chain_id=None,
        tool_catalog={name: definition.render_for_router()},
        events=tool_ctx.events,
    )

    async def _invoker(call_args: dict) -> Any:
        return await invoke_tool(get_default_registry(), name, call_args, tool_ctx)

    return await dispatch_tool(name=name, args=args, ctx=dispatch_ctx, invoker=_invoker)


def _format_task_row(task: dict) -> str:
    task_id = str(task.get("task_id") or "")
    kind = task.get("kind") or "?"
    session = task.get("session") or "?"
    return f"  {task_id}  {kind}  (registered on {session})"


@slash("tasks", summary="List or cancel this session's running tasks (no arg = list)", locus="session", usage=_USAGE)
async def tasks_cmd(ctx: "SlashContext", args: str) -> None:
    arg = (args or "").strip()

    if not arg:
        result = await _dispatch("list_tasks", {}, ctx)
        if result.get("status") == "error":
            await reply_error(ctx, f"/tasks: {result['error']['message']}")
            return
        tasks = result.get("data", {}).get("tasks", [])
        if not tasks:
            await reply(ctx, "no running tasks")
            return
        lines = ["running tasks:"] + [_format_task_row(t) for t in tasks]
        await reply(ctx, "\n".join(lines))
        return

    parts = arg.split(maxsplit=1)
    if parts[0] != "cancel" or len(parts) != 2:
        await reply_error(ctx, _USAGE)
        return

    await _cancel(ctx, parts[1])


async def _cancel(ctx: "SlashContext", supplied: str) -> None:
    """Two-step confirm — mirrors ``/reset``/``/pending discard``'s own
    pattern (``test_slash_destructive_confirm_parity.py``): the first
    invocation warns and takes no action; ``/tasks cancel <id> confirm``
    is what actually calls ``cancel_task``. A misclick on a Tab-completed
    task id must not immediately reach across to another session's
    in-flight turn."""
    stripped = supplied.strip()
    if stripped.lower().endswith(" confirm"):
        task_id = stripped[: -len(" confirm")].strip()
        do_confirm = True
    else:
        task_id = stripped
        do_confirm = False

    if not do_confirm:
        await reply(
            ctx,
            f"⚠ About to request cancellation of task {task_id}.\n"
            f"Type `/tasks cancel {task_id} confirm` to proceed, "
            "or anything else to leave it running.",
        )
        return

    result = await _dispatch("cancel_task", {"task_id": task_id}, ctx)
    if result.get("status") == "error":
        # #3450: a bare `{"error": "..."}` from _handle_cancel_task is
        # promoted to THIS outer envelope by dispatch_tool itself — no
        # separate `data.get("status")` check is reachable here, or ever
        # needed.
        await reply_error(ctx, f"/tasks cancel: {result['error']['message']}")
        return
    await reply(ctx, f"cancel requested for task {task_id}")
