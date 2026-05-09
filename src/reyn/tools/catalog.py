"""catalog ToolDefinitions — ADR-0026 M3 Wave 2 migration.

Covers the 4 catalog-browse capabilities:
  list_skills, describe_skill, list_agents, describe_agent.

Type C closure: both router and phase are allowed (gates.phase="allow"),
enabling skill-author phases to browse the registry in M4. Phase-side
dispatch wiring (= injecting these tools into Control IR
available_control_ops) is deferred to M4.

Design-revisit finding (M4 requirement):
  Each handler requires access to the session-scoped registries
  (skill registry → list_available_skills / describe_skill; agent
  registry → list_available_agents / describe_agent). These are
  supplied by RouterLoopHost and are NOT reachable from ToolContext
  today. Concretely:

    * router caller: RouterLoop.host.list_available_skills() /
                     RouterLoop.host.list_available_agents()
    * phase caller:  OpContext does not expose a skill/agent registry
                     at all.

  A typed RouterCallerState / PhaseCallerState sub-object on
  ToolContext (ADR-0026 Open Question #3) is the clean path. That
  sub-object has landed in M4 Phase 2. Production population of
  RouterCallerState.skill_registry / agent_registry fields (=
  router_loop wiring) is M4 Phase 3. Until Phase 3, these handlers
  raise NotImplementedError.

  RouterLoop continues to dispatch list_skills / describe_skill /
  list_agents / describe_agent via the existing
  ``if name == "list_skills"`` branches in router_loop.py
  (_invoke_router_tool). The ToolDefinitions here serve M3's goal:
  description + parameters + gates registered in the unified registry
  for render / gate / drift checks. M4 Phase 3 will wire the handlers
  to consume RouterCallerState.skill_registry / agent_registry.

Description strings are byte-identical to the ToolSpec literals in
src/reyn/chat/router_tools.py lines 250–324 (Wave 2 validation target).
"""
from __future__ import annotations

from typing import Any, Mapping

from reyn.tools.types import ToolDefinition, ToolGates, ToolContext, ToolResult


# ── list_skills ───────────────────────────────────────────────────────────────

# Byte-identical to router_tools.py ToolSpec description for list_skills
# (lines 253–262). Copied verbatim.
_LIST_SKILLS_DESCRIPTION = (
    "Browse the skill catalogue hierarchically. "
    "Pass empty string to see top-level categories. "
    "Pass a category path to drill in. "
    "Returns either child categories or items, "
    "each with name and one-line description. "
    "After this returns, narrate the skill names directly to "
    "the user in your next message — do not stop after listing "
    "and do not ask for confirmation before naming them."
)

# Byte-identical to router_tools.py ToolSpec parameters for list_skills
# (lines 263–275). Copied verbatim.
_LIST_SKILLS_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "path": {
            "type": "string",
            "description": (
                'Category path, e.g. "", "write", "write/blog". '
                "Empty = root."
            ),
        }
    },
    "required": ["path"],
}


async def _handle_list_skills(args: Mapping[str, Any], ctx: ToolContext) -> ToolResult:
    """Design-revisit stub — not a real dispatch adapter.

    list_skills requires access to the session-scoped skill registry
    via ctx.router_state.skill_registry (RouterCallerState, M4 Phase 2
    structure defined). RouterLoop continues to dispatch list_skills directly
    via the _invoke_router_tool branch until M4 Phase 3 wires the
    RouterCallerState.skill_registry field.
    """
    raise NotImplementedError(
        "list_skills handler is a design-revisit stub: the skill registry "
        "(RouterCallerState.skill_registry) is not yet populated in production. "
        "RouterLoop dispatches list_skills directly until M4 Phase 3 wires "
        "RouterCallerState.skill_registry (ADR-0026 Open Question #3)."
    )


LIST_SKILLS = ToolDefinition(
    name="list_skills",
    description=_LIST_SKILLS_DESCRIPTION,
    parameters=_LIST_SKILLS_PARAMETERS,
    gates=ToolGates(router="allow", phase="allow"),
    handler=_handle_list_skills,
    purity="read_only",
    category="discovery",
)


# ── describe_skill ────────────────────────────────────────────────────────────

# Byte-identical to router_tools.py ToolSpec description for describe_skill
# (lines 279–285). Copied verbatim.
_DESCRIBE_SKILL_DESCRIPTION = (
    "Fetch full metadata for one skill: when_to_use, examples, "
    "input artifact schema. "
    "Call this before invoke_skill if you're unsure how to "
    "construct the input."
)

# Byte-identical to router_tools.py ToolSpec parameters for describe_skill
# (lines 286–292). Copied verbatim.
_DESCRIBE_SKILL_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
    },
    "required": ["name"],
}


async def _handle_describe_skill(args: Mapping[str, Any], ctx: ToolContext) -> ToolResult:
    """Design-revisit stub — not a real dispatch adapter.

    describe_skill requires access to the session-scoped skill registry
    via ctx.router_state.skill_registry (RouterCallerState, M4 Phase 2
    structure defined). RouterLoop continues to dispatch describe_skill directly
    via the _invoke_router_tool branch until M4 Phase 3 wires the
    RouterCallerState.skill_registry field.
    """
    raise NotImplementedError(
        "describe_skill handler is a design-revisit stub: the skill registry "
        "(RouterCallerState.skill_registry) is not yet populated in production. "
        "RouterLoop dispatches describe_skill directly until M4 Phase 3 wires "
        "RouterCallerState.skill_registry (ADR-0026 Open Question #3)."
    )


DESCRIBE_SKILL = ToolDefinition(
    name="describe_skill",
    description=_DESCRIBE_SKILL_DESCRIPTION,
    parameters=_DESCRIBE_SKILL_PARAMETERS,
    gates=ToolGates(router="allow", phase="allow"),
    handler=_handle_describe_skill,
    purity="read_only",
    category="discovery",
)


# ── list_agents ───────────────────────────────────────────────────────────────

# Byte-identical to router_tools.py ToolSpec description for list_agents
# (lines 297–301). Copied verbatim.
_LIST_AGENTS_DESCRIPTION = (
    "Browse peer agents reachable via topology. "
    "Pass empty path for clusters; "
    "pass a cluster name for agents in it."
)

# Byte-identical to router_tools.py ToolSpec parameters for list_agents
# (lines 302–308). Copied verbatim.
_LIST_AGENTS_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "path": {"type": "string"},
    },
    "required": ["path"],
}


async def _handle_list_agents(args: Mapping[str, Any], ctx: ToolContext) -> ToolResult:
    """Design-revisit stub — not a real dispatch adapter.

    list_agents requires access to the session-scoped agent registry
    via ctx.router_state.agent_registry (RouterCallerState, M4 Phase 2
    structure defined). RouterLoop continues to dispatch list_agents directly
    via the _invoke_router_tool branch until M4 Phase 3 wires the
    RouterCallerState.agent_registry field.
    """
    raise NotImplementedError(
        "list_agents handler is a design-revisit stub: the agent registry "
        "(RouterCallerState.agent_registry) is not yet populated in production. "
        "RouterLoop dispatches list_agents directly until M4 Phase 3 wires "
        "RouterCallerState.agent_registry (ADR-0026 Open Question #3)."
    )


LIST_AGENTS = ToolDefinition(
    name="list_agents",
    description=_LIST_AGENTS_DESCRIPTION,
    parameters=_LIST_AGENTS_PARAMETERS,
    gates=ToolGates(router="allow", phase="allow"),
    handler=_handle_list_agents,
    purity="read_only",
    category="discovery",
)


# ── describe_agent ────────────────────────────────────────────────────────────

# Byte-identical to router_tools.py ToolSpec description for describe_agent
# (lines 313–316). Copied verbatim.
_DESCRIBE_AGENT_DESCRIPTION = (
    "Fetch full role / capabilities profile for one agent. "
    "Call before delegate_to_agent if uncertain."
)

# Byte-identical to router_tools.py ToolSpec parameters for describe_agent
# (lines 317–323). Copied verbatim.
_DESCRIBE_AGENT_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
    },
    "required": ["name"],
}


async def _handle_describe_agent(args: Mapping[str, Any], ctx: ToolContext) -> ToolResult:
    """Design-revisit stub — not a real dispatch adapter.

    describe_agent requires access to the session-scoped agent registry
    via ctx.router_state.agent_registry (RouterCallerState, M4 Phase 2
    structure defined). RouterLoop continues to dispatch describe_agent directly
    via the _invoke_router_tool branch until M4 Phase 3 wires the
    RouterCallerState.agent_registry field.
    """
    raise NotImplementedError(
        "describe_agent handler is a design-revisit stub: the agent registry "
        "(RouterCallerState.agent_registry) is not yet populated in production. "
        "RouterLoop dispatches describe_agent directly until M4 Phase 3 wires "
        "RouterCallerState.agent_registry (ADR-0026 Open Question #3)."
    )


DESCRIBE_AGENT = ToolDefinition(
    name="describe_agent",
    description=_DESCRIBE_AGENT_DESCRIPTION,
    parameters=_DESCRIBE_AGENT_PARAMETERS,
    gates=ToolGates(router="allow", phase="allow"),
    handler=_handle_describe_agent,
    purity="read_only",
    category="discovery",
)
