"""delegate_to_agent ToolDefinition — ADR-0026 M3 Wave 1.

Router-only (gates.router=allow, gates.phase=deny).

Async-dispatch semantics (ADR-0026 §6)
---------------------------------------
delegate_to_agent is NOT a request/response tool. RouterLoop calls
self.host.send_to_agent(...) and then *exits*; the peer's reply arrives
in a future RouterLoop invocation via PR14 pending_chain. The handler
below cannot be called as a standalone (args, ctx) → ToolResult adapter
because the actual dispatch is wired into RouterLoop through self.host,
which is not reachable from ToolContext.

Design-revisit needed for Wave 2
---------------------------------
The handler raises NotImplementedError at call time to make this
constraint explicit. The ToolDefinition IS registerable and its
metadata (description, parameters, gates) is correct and usable for:
  - render_for_router() byte-identity checks
  - registry gate assertions
  - build_tools() migration (router only)

M4 Phase 2 (landed): RouterCallerState.send_to_agent is the typed field
for the dispatch callback. Population (= router_loop wiring) is M4 Phase 3.

Until M4 Phase 3 wires send_to_agent on RouterCallerState, the router
dispatcher in RouterLoop MUST NOT invoke DELEGATE_TO_AGENT.handler; it
should continue to call self.host.send_to_agent directly after
name == "delegate_to_agent" detection.
"""
from __future__ import annotations

from typing import Any, Mapping

from reyn.tools.types import ToolDefinition, ToolGates, ToolContext, ToolResult


# Description must be byte-identical to router_tools.py line 403 ToolSpec.
# Copied verbatim from the ToolSpec literal at commit-time.
_DELEGATE_TO_AGENT_DESCRIPTION = "Forward the request to a peer agent."

# Parameters JSON schema must be byte-identical to router_tools.py ToolSpec
# for delegate_to_agent. The "to" property omits the dynamic enum (which is
# built per-call in build_tools based on available_agents); the static
# base type is {"type": "string"}.
_DELEGATE_TO_AGENT_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "to": {
            "type": "string",
            "description": (
                "Target agent name as listed by list_agents."
            ),
        },
        "request": {
            "type": "string",
            "description": (
                "Natural-language request paraphrased "
                "for the peer's context."
            ),
        },
    },
    "required": ["to", "request"],
}


async def _handle(args: Mapping[str, Any], ctx: ToolContext) -> ToolResult:
    """Adapter stub for delegate_to_agent.

    Design-revisit needed for Wave 2 (see module docstring).

    The actual dispatch is wired into RouterLoop via self.host.send_to_agent.
    M4 Phase 2 defines RouterCallerState.send_to_agent as the typed field for
    this callback. This handler cannot be called as a standalone adapter until
    M4 Phase 3 populates RouterCallerState.send_to_agent in the router
    dispatcher.

    Raises:
        NotImplementedError: always — signals the M4 Phase 3 production wiring
            gap to any caller that accidentally routes through the unified handler.
    """
    raise NotImplementedError(
        "delegate_to_agent async dispatch is wired into RouterLoop via "
        "self.host.send_to_agent. RouterCallerState.send_to_agent (M4 Phase 2 "
        "structure) is not yet populated in production (M4 Phase 3). "
        "RouterLoop dispatches delegate_to_agent directly until Phase 3 lands."
    )


DELEGATE_TO_AGENT = ToolDefinition(
    name="delegate_to_agent",
    description=_DELEGATE_TO_AGENT_DESCRIPTION,
    parameters=_DELEGATE_TO_AGENT_PARAMETERS,
    gates=ToolGates(router="allow", phase="deny"),
    handler=_handle,
    category="delegation",
    purity="side_effect",
    dispatch_kind="async",  # PR14 pending_chain: result arrives in future RouterLoop turn
)
