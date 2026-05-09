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

from reyn.tools.types import ToolContext, ToolDefinition, ToolGates, ToolResult

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
    """Delegate to RouterCallerState.list_skills_fn (M4 Phase 3 wiring).

    The caller (= RouterLoop) populates router_state.list_skills_fn with
    a bound method (= RouterLoop._list_skills) at dispatch time. This
    decouples the catalog tool from RouterLoopHost type. If router_state
    or list_skills_fn is None (= mis-wired or test sites that don't
    populate it), raise RuntimeError with a clear message.

    Returns the list directly. ToolResult is typed as Mapping[str, Any] but
    the dispatcher JSON-serializes whatever the handler returns; preserving
    byte-identity with the legacy router branches (which returned bare list)
    is the migration safety mechanism (= LLMReplay fixtures unchanged).
    """
    rs = ctx.router_state
    if rs is None or rs.list_skills_fn is None:
        raise RuntimeError(
            "list_skills handler requires ctx.router_state.list_skills_fn "
            "to be populated by the dispatcher (= RouterLoop)."
        )
    path = args.get("path", "")
    return rs.list_skills_fn(path)  # type: ignore[return-value]


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
    """Delegate to RouterCallerState.describe_skill_fn (M4 Phase 3 wiring).

    The caller (= RouterLoop) populates router_state.describe_skill_fn with
    a bound method (= RouterLoop._describe_skill) at dispatch time. This
    decouples the catalog tool from RouterLoopHost type. If router_state
    or describe_skill_fn is None (= mis-wired or test sites that don't
    populate it), raise RuntimeError with a clear message.

    The underlying fn returns a Mapping directly (single skill dict), so no
    wrapping is needed — return it as-is to satisfy ToolResult = Mapping[str, Any].
    """
    rs = ctx.router_state
    if rs is None or rs.describe_skill_fn is None:
        raise RuntimeError(
            "describe_skill handler requires ctx.router_state.describe_skill_fn "
            "to be populated by the dispatcher (= RouterLoop)."
        )
    name = args.get("name", "")
    return rs.describe_skill_fn(name)


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
    """Delegate to RouterCallerState.list_agents_fn (M4 Phase 3 wiring).

    The caller (= RouterLoop) populates router_state.list_agents_fn with
    a bound method (= RouterLoop._list_agents) at dispatch time. This
    decouples the catalog tool from RouterLoopHost type. If router_state
    or list_agents_fn is None (= mis-wired or test sites that don't
    populate it), raise RuntimeError with a clear message.

    Returns the list directly. ToolResult is typed as Mapping[str, Any] but
    the dispatcher JSON-serializes whatever the handler returns; preserving
    byte-identity with the legacy router branches (which returned bare list)
    is the migration safety mechanism (= LLMReplay fixtures unchanged).
    """
    rs = ctx.router_state
    if rs is None or rs.list_agents_fn is None:
        raise RuntimeError(
            "list_agents handler requires ctx.router_state.list_agents_fn "
            "to be populated by the dispatcher (= RouterLoop)."
        )
    path = args.get("path", "")
    return rs.list_agents_fn(path)  # type: ignore[return-value]


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
    """Delegate to RouterCallerState.describe_agent_fn (M4 Phase 3 wiring).

    The caller (= RouterLoop) populates router_state.describe_agent_fn with
    a bound method (= RouterLoop._describe_agent) at dispatch time. This
    decouples the catalog tool from RouterLoopHost type. If router_state
    or describe_agent_fn is None (= mis-wired or test sites that don't
    populate it), raise RuntimeError with a clear message.

    The underlying fn returns a Mapping directly (single agent dict), so no
    wrapping is needed — return it as-is to satisfy ToolResult = Mapping[str, Any].
    """
    rs = ctx.router_state
    if rs is None or rs.describe_agent_fn is None:
        raise RuntimeError(
            "describe_agent handler requires ctx.router_state.describe_agent_fn "
            "to be populated by the dispatcher (= RouterLoop)."
        )
    name = args.get("name", "")
    return rs.describe_agent_fn(name)


DESCRIBE_AGENT = ToolDefinition(
    name="describe_agent",
    description=_DESCRIBE_AGENT_DESCRIPTION,
    parameters=_DESCRIBE_AGENT_PARAMETERS,
    gates=ToolGates(router="allow", phase="allow"),
    handler=_handle_describe_agent,
    purity="read_only",
    category="discovery",
)
