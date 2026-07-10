"""topology_create ToolDefinition — #2103 C1 (LLM topology-create primitive, org-design).

Router-only (gates.router=allow, gates.phase=deny). The LLM DESIGNS an org's WIRING: it
groups agents it spawned into a topology (who-can-message-whom, by kind) and optionally
binds each member to a capability_profile (narrowing it further). The handler calls
ctx.router_state.topology_create_fn(...) → the host's create-via-topology seam, which
routes through registry.create_topology(topo) — the ONE logged CREATE seam (#2153,
add_topology + emit topology_created), so the topology is WAL-tracked for rewind.

Forge-guard (#2103 C1, lead-approved Q1): the host seam restricts members to the
creator's spawn SUBTREE (itself + transitive spawn-descendants). That makes the profile
bindings safe BY CONSTRUCTION — every bound member is already ⊆ the creator via the
B-core lineage conjunct, so a binding only narrows within that envelope, never re-grants.
The LLM never wires a non-descendant peer it doesn't own. Pairs with agent_spawn:
agent_spawn creates children ⊆ self (identity + lineage); topology_create wires/narrows
THOSE children (the capability assignment) — cleanly split.

The #2081 floor also applies (topology_create is in the floored "spawn" class).
"""
from __future__ import annotations

from typing import Any, Mapping

from reyn.tools.types import ToolContext, ToolDefinition, ToolGates, ToolResult

_TOPOLOGY_CREATE_DESCRIPTION = (
    "Wire agents you spawned into a topology (org-design): group them by kind "
    "(network = all-to-all, team = star around a leader, pipeline = ordered chain) to "
    "control who-can-message-whom, and optionally bind each member to a "
    "capability_profile to narrow it further. You may only include agents in your own "
    "spawn subtree (yourself or agents you created via agent_spawn) — a member's "
    "capabilities stay capped at a SUBSET of yours."
)

_TOPOLOGY_CREATE_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {
            "type": "string",
            "description": "The new topology's name (unique; 1-32 chars [a-z0-9_-]).",
        },
        "kind": {
            "type": "string",
            "enum": ["network", "team", "pipeline"],
            "description": (
                "network = every member ↔ every member; team = star around a leader "
                "(requires 'leader'); pipeline = ordered chain (members[i] → members[i+1])."
            ),
        },
        "members": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Agent names to wire — each must be in your spawn subtree (yourself or "
                "an agent you spawned)."
            ),
        },
        "leader": {
            "type": "string",
            "description": "For kind=team only: the member at the centre of the star.",
        },
        "profiles": {
            "type": "object",
            "description": (
                "Optional JSON object mapping a member name to a capability_profile name "
                "(both strings). A bound member's session is narrowed by that profile (it "
                "can only narrow within its ⊆-you envelope, never widen). Each key must be "
                "one of 'members'."
            ),
        },
    },
    "required": ["name", "kind", "members"],
}


async def _handle(args: Mapping[str, Any], ctx: ToolContext) -> ToolResult:
    """Dispatch to RouterCallerState.topology_create_fn (#2103 C1).

    Returns a create-ack. Raises RuntimeError when the host doesn't support
    topology-create (= mis-wiring / a non-multi-agent host)."""
    rs = ctx.router_state
    if rs is None or getattr(rs, "topology_create_fn", None) is None:
        raise RuntimeError(
            "topology_create requires ctx.router_state.topology_create_fn — unavailable "
            "(host does not support topology-create / mis-wired dispatcher)."
        )
    name = args.get("name")
    if not isinstance(name, str) or not name.strip():
        return {
            "status": "error",
            "kind": "invalid_name",
            "error": "topology_create requires a non-empty 'name'.",
        }
    kind = args.get("kind")
    if not isinstance(kind, str) or not kind.strip():
        return {
            "status": "error",
            "kind": "invalid_kind",
            "error": "topology_create requires a 'kind' (network|team|pipeline).",
        }
    members_raw = args.get("members")
    if not isinstance(members_raw, list) or not members_raw:
        return {
            "status": "error",
            "kind": "invalid_members",
            "error": "topology_create requires a non-empty 'members' list.",
        }
    members = [str(m) for m in members_raw]
    leader = args.get("leader")
    profiles_raw = args.get("profiles") or {}
    profiles = (
        {str(k): str(v) for k, v in profiles_raw.items()}
        if isinstance(profiles_raw, dict)
        else {}
    )
    return await rs.topology_create_fn(
        name=name,
        kind=kind,
        members=members,
        leader=(str(leader) if leader else None),
        profiles=profiles,
    )


from reyn.core.offload.canonical import topology_create_to_canonical  # noqa: E402

TOPOLOGY_CREATE = ToolDefinition(
    canonical=topology_create_to_canonical,
    name="topology_create",
    router_dispatched=True,
    description=_TOPOLOGY_CREATE_DESCRIPTION,
    parameters=_TOPOLOGY_CREATE_PARAMETERS,
    gates=ToolGates(router="allow", phase="deny"),
    handler=_handle,
    category="delegation",
    purity="side_effect",
    dispatch_kind="sync",  # creates the topology + emits topology_created; returns an ack
)
