"""plan ToolDefinition — ADR-0026 M3 Wave 1 migration.

Router-only: gates.router="allow", gates.phase="deny".

Async dispatch posture (ADR-0023 Phase 2.1):
  ``plan`` is fire-and-forget. The real dispatch logic lives in
  ``reyn.chat.planner.dispatch_plan_tool``. The handler returns a spawn
  ack dict; actual progress/result arrives via outbox in future
  RouterLoop turns.

  **M4 Phase 3 (landed)**: handler delegates to
  ``RouterCallerState.dispatch_plan_tool``, populated by RouterLoop with
  all session-scoped state pre-bound:

    * ``parent_host``            (RouterLoopHost — exposes spawn_plan_task,
                                  write_plan_decomposition, budget, router_model)
    * ``chain_id``               (chat-turn chain for parent_chain_id hand-off)
    * ``budget``                 (BudgetGateway instance)
    * ``router_model``           (model string)
    * ``available_tool_names``   (dynamic per-session; needed for cycle
                                  detection / plan validation)

  RouterLoop is responsible for binding session state at population time
  (e.g. via ``functools.partial`` or a closure), keeping the handler
  signature pure ``(args, ctx)``. The handler passes only ``args``
  to the pre-bound callable.

  WHY this design: ``dispatch_plan_tool`` requires inherently
  router-session-scoped state that does not map cleanly to
  ToolContext's protocol-agnostic surface. Binding at RouterLoop
  population time keeps the handler decoupled from session internals
  while enabling full async-dispatch semantics.

Description and parameters are byte-identical to the ToolSpec literal
in router_tools.py line 686–751.
"""
from __future__ import annotations

from typing import Any, Mapping

from reyn.tools.types import ToolContext, ToolDefinition, ToolGates, ToolResult

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
    "directly or call one tool; do NOT use plan. "
    "Each step summarises what it found; the router "
    "synthesises the final reply after all steps "
    "complete."
)

# Assertive WHAT/WHEN/WHEN_NOT description used when hide_legacy_tools=True.
# (B23-PRE-1 SP role-separation: ## Plan decomposition SP subsection
# moves here; SP Behaviour retains only the 2-line multi-source routing
# policy as a cross-cutting rule.)
_PLAN_DESCRIPTION_HIDE_LEGACY = (
    "WHAT: Decompose a multi-source query into discrete steps the OS executes "
    "sequentially, then synthesize the final reply. "
    "WHEN: When the query combines info from multiple independent sources "
    "(e.g. 'compare A and B from two docs', 'explain X with code refs from N "
    "files', 'summarise across these sources'). Each step gathers one piece; "
    "OS synthesizes. "
    "WHEN NOT: "
    "- Single-tool lookups or single-source narrations. "
    "- Chitchat or conversational replies. "
    "- Queries that invoke_action handles end-to-end. "
    "- Queries answerable in one router reply without tools."
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
                "\"invoke_skill\"). Use [] for steps that only "
                "need prior step outputs as context — the step "
                "LLM reasons from those natively. To run a skill, "
                "use [\"invoke_skill\"], NOT the skill's name. "
                "depends_on: ids of prior steps whose output this "
                "step needs (default []). Each step should "
                "summarise what it found; the router synthesises "
                "the final reply after all steps complete. Example: "
                "[{\"id\": \"s1\", \"description\": \"read README\", "
                "\"tools\": [\"reyn_src_read\"], \"depends_on\": []}, "
                "{\"id\": \"s2\", \"description\": \"compare and "
                "summarise findings\", "
                "\"tools\": [], \"depends_on\": [\"s1\"]}]"
            ),
        },
    },
    "required": ["goal", "steps_json"],
}


async def _handle(args: Mapping[str, Any], ctx: ToolContext) -> ToolResult:
    """Delegate to RouterCallerState.dispatch_plan_tool (M4 Phase 3 wiring).

    See module docstring for the async-dispatch posture. The
    dispatch_plan_tool callable is populated by RouterLoop with all
    session-scoped state (parent_host, chain_id, budget, router_model,
    available_tool_names) pre-bound; the handler passes only args.
    """
    rs = ctx.router_state
    if rs is None or rs.dispatch_plan_tool is None:
        raise RuntimeError(
            "plan handler requires ctx.router_state.dispatch_plan_tool "
            "to be populated by the dispatcher (= RouterLoop)."
        )
    return await rs.dispatch_plan_tool(args=args)


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
