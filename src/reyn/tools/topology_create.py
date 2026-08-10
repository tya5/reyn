"""create_topology ToolDefinition — #2103 C1 (LLM topology-create primitive, org-design).
Renamed from topology_create (#4004) — the module and its internal
identifiers keep the old spelling; only the registered ``name=`` (the
LLM-visible string) and everything keyed off it changed.

Router-only (gates.router=allow). The LLM DESIGNS an org's WIRING: it
groups agents it spawned into a topology (who-can-message-whom, by kind) and optionally
binds each member to a capability_profile (narrowing it further). The handler calls
ctx.router_state.topology_create_fn(...) → the host's create-via-topology seam, which
routes through registry.create_topology(topo) — the ONE logged CREATE seam (#2153,
add_topology + emit topology_created), so the topology is WAL-tracked for rewind.

Forge-guard (#2103 C1, lead-approved Q1): the host seam restricts members to the
creator's spawn SUBTREE (itself + transitive spawn-descendants). That makes the profile
bindings safe BY CONSTRUCTION — every bound member is already ⊆ the creator via the
B-core lineage conjunct, so a binding only narrows within that envelope, never re-grants.
The LLM never wires a non-descendant peer it doesn't own. Pairs with spawn_agent:
spawn_agent creates children ⊆ self (identity + lineage); create_topology wires/narrows
THOSE children (the capability assignment) — cleanly split.

The #2081 floor also applies (create_topology is in the floored "spawn" class).
"""
from __future__ import annotations

from typing import Any, Mapping

from reyn.tools.descriptions import delegation as _delegation_descriptions
from reyn.tools.types import ToolContext, ToolDefinition, ToolGates, ToolResult

# Reviewable in src/reyn/tools/descriptions/delegation.py (Phase 2 of the
# tool-description package refactor) — this alias keeps the call site
# unchanged (byte-identical relocation, no LLM-facing text change).
_TOPOLOGY_CREATE_DESCRIPTION = _delegation_descriptions.topology_create.text

_TOPOLOGY_CREATE_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {
            "type": "string",
            "description": _delegation_descriptions.PARAMS["create_topology"]["name"].text,
        },
        "kind": {
            "type": "string",
            "enum": ["network", "team", "pipeline"],
            "description": _delegation_descriptions.PARAMS["create_topology"]["kind"].text,
        },
        "members": {
            "type": "array",
            "items": {"type": "string"},
            "description": _delegation_descriptions.PARAMS["create_topology"]["members"].text,
        },
        "leader": {
            "type": "string",
            "description": _delegation_descriptions.PARAMS["create_topology"]["leader"].text,
        },
        "profiles": {
            "type": "object",
            "description": _delegation_descriptions.PARAMS["create_topology"]["profiles"].text,
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
            "create_topology requires ctx.router_state.topology_create_fn — unavailable "
            "(host does not support topology-create / mis-wired dispatcher)."
        )
    name = args.get("name")
    if not isinstance(name, str) or not name.strip():
        return {
            "status": "error",
            "kind": "invalid_name",
            "error": "create_topology requires a non-empty 'name'.",
        }
    kind = args.get("kind")
    if not isinstance(kind, str) or not kind.strip():
        return {
            "status": "error",
            "kind": "invalid_kind",
            "error": "create_topology requires a 'kind' (network|team|pipeline).",
        }
    members_raw = args.get("members")
    if not isinstance(members_raw, list) or not members_raw:
        return {
            "status": "error",
            "kind": "invalid_members",
            "error": "create_topology requires a non-empty 'members' list.",
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
    name="create_topology",
    router_dispatched=True,
    description=_TOPOLOGY_CREATE_DESCRIPTION,
    parameters=_TOPOLOGY_CREATE_PARAMETERS,
    gates=ToolGates(router="allow"),
    handler=_handle,
    category="delegation",
    purity="side_effect",
    dispatch_kind="sync",  # creates the topology + emits topology_created; returns an ack
)
