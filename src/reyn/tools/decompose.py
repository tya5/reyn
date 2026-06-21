"""decompose ToolDefinition — #1953 slice P3 (task-driven decomposition entry).

Router-only: gates.router="allow", gates.phase="deny". The task-subsystem analog
of ``plan``, exposed in parallel with it during slice P so the two execution
engines can be compared for behavioral parity (the P4 delete gate). The handler
delegates to ``RouterCallerState.dispatch_task_tool``, populated by RouterLoop
with the session-scoped state pre-bound (parent_host, chain_id, budget,
router_model, available_tool_names, task_backend, assignee, requester) — the same
binding posture as ``plan``'s ``dispatch_plan_tool``. Fire-and-forget: the handler
returns a spawn/completion ack; the synthesized reply arrives via outbox.

Schema mirrors ``plan`` (goal + JSON-encoded steps) so a decomposition emitted for
one tool validates against the other — the parity comparison feeds both engines
the same decomposition.
"""
from __future__ import annotations

from typing import Any, Mapping

from reyn.tools.types import ToolContext, ToolDefinition, ToolGates, ToolResult

_DECOMPOSE_DESCRIPTION = (
    "Decompose a complex query into 2-7 independent sub-tasks, each tracked as a "
    "first-class Task. Use ONLY when the query needs multi-source synthesis (e.g. "
    "\"explain X with code references\", \"compare A vs B from multiple docs\", "
    "\"build a summary across these N files\"). For simple queries — chitchat, "
    "single-tool retrieval, single-source narration — reply directly or call one "
    "tool; do NOT decompose. After all sub-tasks complete, the synthesized reply "
    "is the final-sub-task result. Design each sub-task to gather concrete "
    "evidence (code snippets, file excerpts, specific facts) — not a summary."
)

_DECOMPOSE_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "goal": {
            "type": "string",
            "description": (
                "1-sentence restatement of the user's overall query."
            ),
        },
        "steps_json": {
            "type": "string",
            "description": (
                "JSON-encoded array of 2-7 sub-task objects. Each has shape: "
                "{\"id\": str, \"description\": str, "
                "\"tools\": [str, ...], \"depends_on\": [str, ...]}. "
                "id: short unique identifier. description: what this sub-task does. "
                "tools: list of TOP-LEVEL router-tool names this sub-task calls — "
                "use the names EXACTLY as they appear in your available tools list "
                "(do not invent names, and do not use a skill's or action's bare "
                "name). Use [] for sub-tasks that only need prior results as "
                "context. depends_on: ids of prior sub-tasks whose output this one "
                "needs (default []). Each sub-task should report concrete evidence; "
                "the final sub-task's result is the synthesized reply. Example: "
                "[{\"id\": \"s1\", \"description\": \"read the project README\", "
                "\"tools\": [\"<one of your available tool names>\"], "
                "\"depends_on\": []}, {\"id\": \"s2\", \"description\": \"report "
                "differences between s1 findings\", \"tools\": [], "
                "\"depends_on\": [\"s1\"]}]"
            ),
        },
    },
    "required": ["goal", "steps_json"],
}


async def _handle(args: Mapping[str, Any], ctx: ToolContext) -> ToolResult:
    """Delegate to RouterCallerState.dispatch_task_tool (populated by RouterLoop
    with all session-scoped state pre-bound; the handler passes only args)."""
    rs = ctx.router_state
    if rs is None or rs.dispatch_task_tool is None:
        raise RuntimeError(
            "decompose handler requires ctx.router_state.dispatch_task_tool "
            "to be populated by the dispatcher (= RouterLoop)."
        )
    return await rs.dispatch_task_tool(args=dict(args))


DECOMPOSE = ToolDefinition(
    name="decompose",
    description=_DECOMPOSE_DESCRIPTION,
    parameters=_DECOMPOSE_PARAMETERS,
    gates=ToolGates(router="allow", phase="deny"),
    handler=_handle,
    category="orchestration",
    purity="side_effect",   # creates a Task DAG, runs the exec engine
    dispatch_kind="async",  # fire-and-forget; synthesized reply via outbox
)
