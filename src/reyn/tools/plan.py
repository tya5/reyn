"""plan ToolDefinition — ADR-0026 M3 Wave 1 migration.

Router-only: gates.router="allow", gates.phase="deny".

Async dispatch posture (ADR-0023 Phase 2.1):
  ``plan`` is fire-and-forget. The real dispatch logic lives in
  ``reyn.chat.planner.dispatch_plan_tool``, which requires caller-side
  state that ToolContext cannot supply today:

    * ``parent_host``   (RouterLoopHost — exposes spawn_plan_task,
                         write_plan_decomposition, budget, router_model)
    * ``chain_id``      (chat-turn chain for parent_chain_id hand-off)
    * ``available_tool_names`` (dynamic per-session; needed for cycle
                                detection / plan validation)

  These are inherently router-session-scoped and do not map cleanly to
  ToolContext's current protocol-agnostic surface.

  **M4 Phase 2 (landed)**: ``RouterCallerState`` typed sub-object on
  ToolContext is now defined. Relevant fields:
    * ``RouterCallerState.dispatch_plan_tool``  (= dispatch_plan_tool callback)
    * ``RouterCallerState.chain_id``            (= parent chain_id)
    * ``RouterCallerState.available_tool_names`` (= dynamic per-session)
    * ``RouterCallerState.budget``              (= BudgetGateway instance)
    * ``RouterCallerState.router_model``        (= model string)

  **Design-revisit finding**: the handler registered here raises
  ``NotImplementedError`` to make the constraint explicit.  RouterLoop
  continues to invoke ``dispatch_plan_tool`` directly (via the existing
  ``if name == "plan"`` branch in router_loop.py).  M4 Phase 3 will
  populate these fields in RouterCallerState and wire the handler.
  Until Phase 3 lands, this ToolDefinition serves Wave 1's goal:
  description + parameters + gates registered in the unified registry
  for render / gate / drift checks, without forcing a premature adapter.

Description and parameters are byte-identical to the ToolSpec literal
in router_tools.py line 686–751.
"""
from __future__ import annotations

from typing import Any, Mapping

from reyn.tools.types import ToolDefinition, ToolGates, ToolContext, ToolResult


# Description must be byte-identical to the ToolSpec.description for
# plan in router_tools.py (line 687–699).  Copied verbatim.
_PLAN_DESCRIPTION = (
    "Decompose a complex query into 2-7 independent "
    "sub-tasks. Use ONLY when the query needs multi-"
    "source synthesis (e.g. \"explain X with code "
    "references\", \"compare A vs B from multiple "
    "docs\", \"build a summary across these N "
    "files\"). For simple queries — chitchat, single-"
    "tool retrieval, single-source narration — reply "
    "directly or call one tool; do NOT use plan. The "
    "terminal step's text reply becomes the user-"
    "facing answer; design the last step to "
    "synthesise."
)

# Parameters schema must be byte-identical to router_tools.py line
# 700–748.  Copied verbatim (steps_json description includes the full
# inline example).
_PLAN_PARAMETERS: dict[str, Any] = {
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
                "JSON-encoded array of 2-7 step objects. Each "
                "step has shape: "
                "{\"id\": str, \"description\": str, "
                "\"tools\": [str, ...], \"depends_on\": [str, ...]}. "
                "id: short unique identifier. description: what "
                "this step does. "
                "tools: list of TOP-LEVEL tool names this step "
                "calls (e.g. \"reyn_src_read\", \"web_search\", "
                "\"invoke_skill\"). Use [] for steps that just "
                "synthesise / compare / summarise from prior step "
                "outputs — the step's LLM does that natively without "
                "any tool. To run a skill, use [\"invoke_skill\"], "
                "NOT the skill's name. depends_on: ids of prior "
                "steps whose output this step needs (default []). "
                "The terminal step's text reply becomes the user-"
                "facing answer; design the last step to "
                "synthesise (= tools: []). Example: "
                "[{\"id\": \"s1\", \"description\": \"read README\", "
                "\"tools\": [\"reyn_src_read\"], \"depends_on\": []}, "
                "{\"id\": \"s2\", \"description\": \"compare and "
                "summarise for user\", "
                "\"tools\": [], \"depends_on\": [\"s1\"]}]"
            ),
        },
    },
    "required": ["goal", "steps_json"],
}


async def _handle(args: Mapping[str, Any], ctx: ToolContext) -> ToolResult:
    """Design-revisit stub — not a real dispatch adapter.

    ``dispatch_plan_tool`` requires RouterLoopHost, chain_id,
    available_tool_names, budget, and router_model — all now typed on
    RouterCallerState (M4 Phase 2). RouterCallerState.dispatch_plan_tool,
    .chain_id, .available_tool_names, .budget, and .router_model are the
    target fields. Production population is M4 Phase 3.

    RouterLoop continues to call dispatch_plan_tool directly until M4 Phase 3
    populates RouterCallerState and wires the handler.
    """
    raise NotImplementedError(
        "plan handler is a design-revisit stub: RouterCallerState fields "
        "(dispatch_plan_tool, chain_id, available_tool_names, budget, "
        "router_model) are defined (M4 Phase 2) but not yet populated in "
        "production (M4 Phase 3). RouterLoop dispatches plan directly "
        "until Phase 3 lands (ADR-0026 Open Question #3 resolved Phase 2)."
    )


PLAN = ToolDefinition(
    name="plan",
    description=_PLAN_DESCRIPTION,
    parameters=_PLAN_PARAMETERS,
    gates=ToolGates(router="allow", phase="deny"),
    handler=_handle,
    category="orchestration",
    purity="side_effect",   # spawns PlanRuntime task, modifies running_plans
    dispatch_kind="async",  # ADR-0023 Phase 2.1: fire-and-forget; result via outbox
)
