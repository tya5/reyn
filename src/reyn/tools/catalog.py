"""catalog ToolDefinitions — ADR-0026 M3 Wave 2 migration.

Covers the catalog-browse capabilities:
  list_agents, describe_agent.

Type C closure: both router and phase are allowed (gates.phase="allow").
The phase=allow gate is a metadata closure — phase Control IR currently
emits only coarse op.kind values defined in OP_KIND_MODEL_MAP, and the
fine-grained catalog names (``list_agents`` / ``describe_agent``) are not
in that map, so the phase path of these handlers is unreachable today.
Reachable phase invocation would require a separate Control IR schema
migration to fine-grained ``op.kind`` values (out of scope for ADR-0026 M4).

Router-side dispatch (post-FP-0039 audit, 2026-05-18):
  Each handler delegates to a session-scoped function bound on
  ``ctx.router_state`` (``list_agents_fn`` / ``describe_agent_fn``).
  RouterLoop populates these from its own bound methods at dispatch
  time — see ``router_host_adapter.py`` for the typed wiring.

Description strings are byte-identical to the ToolSpec literals in
src/reyn/runtime/router_tools.py (Wave 2 validation target).
"""
from __future__ import annotations

from typing import Any, Mapping

from reyn.tools.descriptions import catalog as _catalog_descriptions
from reyn.tools.types import ToolContext, ToolDefinition, ToolGates, ToolResult

# ── list_agents ───────────────────────────────────────────────────────────────

# Relocated to reyn.tools.descriptions.catalog (Phase 3 tool-description
# package refactor — byte-identical, no LLM-facing text change).
_LIST_AGENTS_DESCRIPTION = _catalog_descriptions.list_agents.text

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


from reyn.core.offload.canonical import list_agents_to_canonical  # noqa: E402

LIST_AGENTS = ToolDefinition(
    canonical=list_agents_to_canonical,
    name="list_agents",
    router_dispatched=True,
    description=_LIST_AGENTS_DESCRIPTION,
    parameters=_LIST_AGENTS_PARAMETERS,
    gates=ToolGates(router="allow", phase="allow"),
    handler=_handle_list_agents,
    purity="read_only",
    category="discovery",
)


# ── describe_agent ────────────────────────────────────────────────────────────

# Relocated to reyn.tools.descriptions.catalog (Phase 3 tool-description
# package refactor — byte-identical, no LLM-facing text change).
_DESCRIBE_AGENT_DESCRIPTION = _catalog_descriptions.describe_agent.text

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


from reyn.core.offload.canonical import describe_agent_to_canonical  # noqa: E402

DESCRIBE_AGENT = ToolDefinition(
    canonical=describe_agent_to_canonical,
    name="describe_agent",
    router_dispatched=True,
    description=_DESCRIBE_AGENT_DESCRIPTION,
    parameters=_DESCRIBE_AGENT_PARAMETERS,
    gates=ToolGates(router="allow", phase="allow"),
    handler=_handle_describe_agent,
    purity="read_only",
    category="discovery",
)
