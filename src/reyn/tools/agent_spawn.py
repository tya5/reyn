"""agent_spawn ToolDefinition — #2103 B-tool (LLM agent-spawn primitive, org-design).

Router-only (gates.router=allow, gates.phase=deny). The LLM DESIGNS an org: it creates
a new AGENT (WHO: name + role) under its own authority. The handler calls
ctx.router_state.spawn_agent_fn(...) → the host's create-via-spawn seam, which routes
through registry.create_agent(parent=<the spawner>) so the new agent's spawn LINEAGE is
OS-SET (immutable; the LLM never supplies the parent link — the forge-guard).

Capability model (#2103 B-core, ⊆-parent by construction): the spawned agent's effective
capability is CAPPED at ⊆ the spawner — resolved_profile_for composes the spawner's LIVE
resolved effective as a restrict-only conjunct, so the new agent can never exceed the
spawner (recursive, no-escalation-via-spawn). The #2081 floor also applies (least-
privilege). Narrowing the child BELOW the spawner is done via ``topology_create`` (C),
which assigns the restrict-only capability_profile bindings — keeping agent-spawn
(identity + lineage) and the capability assignment (topology profiles) cleanly split.
"""
from __future__ import annotations

from typing import Any, Mapping

from reyn.tools.descriptions import delegation as _delegation_descriptions
from reyn.tools.types import ToolContext, ToolDefinition, ToolGates, ToolResult

# Reviewable in src/reyn/tools/descriptions/delegation.py (Phase 2 of the
# tool-description package refactor) — this alias keeps the call site
# unchanged (byte-identical relocation, no LLM-facing text change).
_AGENT_SPAWN_DESCRIPTION = _delegation_descriptions.agent_spawn.text

_AGENT_SPAWN_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {
            "type": "string",
            "description": "The new agent's identity (a unique agent name).",
        },
        "role": {
            "type": "string",
            "description": "The new agent's role/purpose (free text).",
        },
    },
    "required": ["name"],
}


async def _handle(args: Mapping[str, Any], ctx: ToolContext) -> ToolResult:
    """Dispatch to RouterCallerState.spawn_agent_fn (#2103 B-tool).

    Returns a spawn-ack. Raises RuntimeError when the host doesn't support agent-spawn
    (= mis-wiring / a non-multi-agent host)."""
    rs = ctx.router_state
    if rs is None or getattr(rs, "spawn_agent_fn", None) is None:
        raise RuntimeError(
            "agent_spawn requires ctx.router_state.spawn_agent_fn — unavailable "
            "(host does not support agent-spawn / mis-wired dispatcher)."
        )
    name = args.get("name")
    if not isinstance(name, str) or not name.strip():
        return {
            "status": "error",
            "kind": "invalid_name",
            "error": "agent_spawn requires a non-empty 'name'.",
        }
    return await rs.spawn_agent_fn(name=name, role=args.get("role", "") or "")


from reyn.core.offload.canonical import agent_spawn_to_canonical  # noqa: E402

AGENT_SPAWN = ToolDefinition(
    canonical=agent_spawn_to_canonical,
    name="agent_spawn",
    router_dispatched=True,
    description=_AGENT_SPAWN_DESCRIPTION,
    parameters=_AGENT_SPAWN_PARAMETERS,
    gates=ToolGates(router="allow", phase="deny"),
    handler=_handle,
    category="delegation",
    purity="side_effect",
    dispatch_kind="sync",  # creates the agent + records lineage; returns a spawn-ack
)
