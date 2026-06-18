"""catalog ToolDefinitions — ADR-0026 M3 Wave 2 migration.

Covers the 4 catalog-browse capabilities:
  list_skills, describe_skill, list_agents, describe_agent.

Type C closure: both router and phase are allowed (gates.phase="allow").
The phase=allow gate is a metadata closure — phase Control IR currently
emits only coarse op.kind values defined in OP_KIND_MODEL_MAP, and the
fine-grained catalog names (``list_skills`` / ``describe_skill`` /
``list_agents`` / ``describe_agent``) are not in that map, so the phase
path of these handlers is unreachable today.  Reachable phase invocation
would require a separate Control IR schema migration to fine-grained
``op.kind`` values (out of scope for ADR-0026 M4).

Router-side dispatch (post-FP-0039 audit, 2026-05-18):
  Each handler delegates to a session-scoped function bound on
  ``ctx.router_state`` (``list_skills_fn`` / ``describe_skill_fn`` /
  ``list_agents_fn`` / ``describe_agent_fn``).  RouterLoop populates
  these from its own bound methods at dispatch time — see
  ``router_host_adapter.py`` for the typed wiring.

  Pre-Phase-3 ``if name == "list_skills"`` literal branches in
  router_loop.py have been removed; the registry handler is the
  single dispatch path.

Description strings are byte-identical to the ToolSpec literals in
src/reyn/runtime/router_tools.py lines 250–324 (Wave 2 validation target).
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
    """Delegate to RouterCallerState.list_skills_fn.

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
    """Delegate to RouterCallerState.describe_skill_fn.

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
    """Delegate to RouterCallerState.list_agents_fn.

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
    """Delegate to RouterCallerState.describe_agent_fn.

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
