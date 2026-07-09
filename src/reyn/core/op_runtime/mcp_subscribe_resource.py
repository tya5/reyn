"""mcp_subscribe_resource kind handler — subscribe to a resource's
``resources/updated`` push notifications on a configured MCP server.

#2597 slice ②b (resource subscriptions). Mirrors
``op_runtime/mcp_read_resource.py`` exactly: same server-config resolution,
same ``MCPGateway`` seam, same ``require_mcp`` permission gate (server-scoped —
subscribing is a stateful action against the server, gated identically to a
read), and the same before/after event pair shape.

Requires ``ctx.mcp_connection_service`` specifically (not ``ctx.mcp_pool``) —
a subscription is only meaningful on a HELD (persistent) connection; the
session-level caller (``session.py``'s ``_mcp_subscribe_resource``) already
refuses this op for an ephemeral session before an ``Op`` is even built, but
this handler re-checks defensively (an ``OpContext`` built any other way with
only ``mcp_pool`` set must not silently "succeed" a subscribe that dies the
instant the one-shot pool closes).

The push notification itself lands as an ``mcp_resource_updated`` EventLog
event via ``reyn.mcp.message_handler.ReynMCPMessageHandler.on_resource_updated``
— NOT as this op's return value, which only confirms the subscribe request
itself succeeded.
"""
from __future__ import annotations

from reyn.schemas.models import MCPSubscribeResourceIROp

from . import register
from .context import OpContext


async def _execute(op: MCPSubscribeResourceIROp, ctx: OpContext) -> dict:
    from reyn.mcp.client import expand_env
    from reyn.mcp.gateway import MCPFault, MCPGateway

    server_cfg = ctx.mcp_servers.get(op.server)
    if not server_cfg:
        return {
            "kind": "mcp_subscribe_resource", "status": "error",
            "error": f"MCP server '{op.server}' is not configured. "
                     f"Add it under mcp.servers in reyn.yaml or reyn.local.yaml.",
        }

    expanded = expand_env(server_cfg)
    if not isinstance(expanded, dict):
        return {"kind": "mcp_subscribe_resource", "status": "error",
                "error": f"MCP server '{op.server}' config must be a dict."}

    if "type" not in expanded and expanded.get("url"):
        expanded = {**expanded, "type": "http"}

    if ctx.mcp_connection_service is None:
        return {
            "kind": "mcp_subscribe_resource", "status": "error", "server": op.server,
            "uri": op.uri,
            "error": "MCP resource subscriptions require a held (persistent) "
                     "connection — no MCPConnectionService on this context.",
        }

    ctx.events.emit("mcp_resource_subscribe", server=op.server, uri=op.uri)
    gateway = MCPGateway(pool=ctx.mcp_connection_service, agent_id=ctx.agent_id)
    try:
        await gateway.subscribe_resource(op.server, op.uri, expanded)
    except MCPFault as fault_exc:
        fault = str(fault_exc)
        ctx.events.emit(
            "mcp_resource_subscribe_failed", server=op.server, uri=op.uri, error=fault,
        )
        return {"kind": "mcp_subscribe_resource", "status": "error", "server": op.server,
                "uri": op.uri, "error": fault}

    ctx.events.emit("mcp_resource_subscribed", server=op.server, uri=op.uri)
    return {
        "kind": "mcp_subscribe_resource",
        "status": "ok",
        "server": op.server,
        "uri": op.uri,
    }


async def handle(op: MCPSubscribeResourceIROp, ctx: OpContext) -> dict:
    if ctx.permission_resolver is not None:
        if ctx.intervention_bus is None:
            raise RuntimeError("mcp_subscribe_resource op requires intervention_bus on OpContext")
        await ctx.permission_resolver.require_mcp(
            ctx.permission_decl, op.server, ctx.intervention_bus,
            contextual=ctx.contextual_permission,
        )
    return await _execute(op, ctx)


from reyn.core.offload.canonical import STRUCTURED_PASSTHROUGH  # noqa: E402

register("mcp_subscribe_resource", handle, canonical=STRUCTURED_PASSTHROUGH)
