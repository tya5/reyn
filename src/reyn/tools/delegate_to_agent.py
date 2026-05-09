"""delegate_to_agent ToolDefinition — ADR-0026 M4 Phase 3.

Router-only (gates.router=allow, gates.phase=deny).

Async-dispatch semantics (ADR-0026 §6)
---------------------------------------
delegate_to_agent is NOT a request/response tool. RouterLoop calls
self.host.send_to_agent(...) and then *exits*; the peer's reply arrives
in a future RouterLoop invocation via PR14 pending_chain.

M4 Phase 3 activation (this file)
-----------------------------------
RouterCallerState.send_to_agent (defined in Wave 1) is now consumed by
_handle. The handler calls ctx.router_state.send_to_agent(to=, request=)
and returns a spawn-ack dict immediately. The actual peer reply arrives
asynchronously in a future RouterLoop turn.

RouterLoop still branches on name == "delegate_to_agent" to call
self.host.send_to_agent directly (legacy path). Wave 3 will migrate
that branch to route through DELEGATE_TO_AGENT.handler via the unified
dispatch path. Until then, both paths are correct:
  - Legacy (RouterLoop direct): used in production today
  - Unified (this handler): activated for test / dispatch-table routes

schema_enricher (_enrich_router_schema) injects `to.enum` from
RouterCallerState.available_agents per-call, replacing the prior inline
_delegate_to_schema literal in router_tools.py.
"""
from __future__ import annotations

import copy
from typing import TYPE_CHECKING, Any, Mapping

from reyn.tools.types import ToolContext, ToolDefinition, ToolGates, ToolResult

if TYPE_CHECKING:
    from reyn.tools.types import RouterCallerState


# Description must be byte-identical to router_tools.py line 403 ToolSpec.
# Copied verbatim from the ToolSpec literal at commit-time.
_DELEGATE_TO_AGENT_DESCRIPTION = "Forward the request to a peer agent."

# Parameters JSON schema must be byte-identical to router_tools.py ToolSpec
# for delegate_to_agent. The "to" property omits the dynamic enum (which is
# injected per-call by _enrich_router_schema from available_agents); the
# static base type is {"type": "string"}.
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


def _enrich_router_schema(rendered: dict, state: "RouterCallerState") -> dict:
    """Inject `to` enum from available_agents (= dynamic per-session data).

    Matches the prior inline literal in router_tools.py: when there's at
    least one agent, the to field gets an enum constraint. When there
    are zero agents, the schema falls back to plain string (no enum).

    Returns a NEW dict — does not mutate the input.
    """
    available_agents = state.available_agents or []
    agent_names = [a["name"] for a in available_agents if "name" in a]
    new = copy.deepcopy(rendered)
    to_prop = new["function"]["parameters"]["properties"].get("to")
    if to_prop is None:
        return new  # defensive: schema is missing the to field
    if agent_names:
        to_prop["enum"] = agent_names
    else:
        to_prop.pop("enum", None)
    return new


async def _handle(args: Mapping[str, Any], ctx: ToolContext) -> ToolResult:
    """Delegate to RouterCallerState.send_to_agent (M4 Phase 3 wiring).

    The send_to_agent callable is populated by RouterLoop with chain_id
    pre-bound; this handler passes only the per-call args (to + request).
    Async-dispatch posture: returns a spawn ack immediately; the actual
    peer reply arrives via PR14 pending_chain in a future RouterLoop turn.

    Raises RuntimeError when router_state or send_to_agent is missing
    (= mis-wiring; matches the catalog/plan handler convention).
    """
    rs = ctx.router_state
    if rs is None or rs.send_to_agent is None:
        raise RuntimeError(
            "delegate_to_agent handler requires ctx.router_state.send_to_agent "
            "to be populated by the dispatcher (= RouterLoop)."
        )
    await rs.send_to_agent(to=args["to"], request=args["request"])
    return {
        "status": "dispatched",
        "to": args["to"],
        "note": (
            "Peer's reply will arrive in a future router invocation; "
            "please wait for it."
        ),
    }


DELEGATE_TO_AGENT = ToolDefinition(
    name="delegate_to_agent",
    description=_DELEGATE_TO_AGENT_DESCRIPTION,
    parameters=_DELEGATE_TO_AGENT_PARAMETERS,
    gates=ToolGates(router="allow", phase="deny"),
    handler=_handle,
    category="delegation",
    purity="side_effect",
    dispatch_kind="async",  # PR14 pending_chain: result arrives in future RouterLoop turn
    schema_enricher=_enrich_router_schema,
)
