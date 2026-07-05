"""mcp_unsubscribe_resource kind handler — inverse of mcp_subscribe_resource.

#2597 slice ②b. Mirrors ``op_runtime/mcp_subscribe_resource.py`` exactly —
see that module's docstring for the full rationale (persistent-connection
requirement, permission gate, event shape).
"""
from __future__ import annotations

from reyn.schemas.models import MCPUnsubscribeResourceIROp

from . import register
from .context import OpContext


async def _execute(op: MCPUnsubscribeResourceIROp, ctx: OpContext) -> dict:
    from reyn.mcp.client import expand_env
    from reyn.mcp.gateway import MCPFault, MCPGateway

    server_cfg = ctx.mcp_servers.get(op.server)
    if not server_cfg:
        return {
            "kind": "mcp_unsubscribe_resource", "status": "error",
            "error": f"MCP server '{op.server}' is not configured. "
                     f"Add it under mcp.servers in reyn.yaml or reyn.local.yaml.",
        }

    expanded = expand_env(server_cfg)
    if not isinstance(expanded, dict):
        return {"kind": "mcp_unsubscribe_resource", "status": "error",
                "error": f"MCP server '{op.server}' config must be a dict."}

    if "type" not in expanded and expanded.get("url"):
        expanded = {**expanded, "type": "http"}

    if ctx.mcp_connection_service is None:
        return {
            "kind": "mcp_unsubscribe_resource", "status": "error", "server": op.server,
            "uri": op.uri,
            "error": "MCP resource subscriptions require a held (persistent) "
                     "connection — no MCPConnectionService on this context.",
        }

    ctx.events.emit("mcp_resource_unsubscribe", server=op.server, uri=op.uri)
    gateway = MCPGateway(pool=ctx.mcp_connection_service, agent_id=ctx.agent_id)
    try:
        await gateway.unsubscribe_resource(op.server, op.uri, expanded)
    except MCPFault as fault_exc:
        fault = str(fault_exc)
        ctx.events.emit(
            "mcp_resource_unsubscribe_failed", server=op.server, uri=op.uri, error=fault,
        )
        return {"kind": "mcp_unsubscribe_resource", "status": "error", "server": op.server,
                "uri": op.uri, "error": fault}

    ctx.events.emit("mcp_resource_unsubscribed", server=op.server, uri=op.uri)
    return {
        "kind": "mcp_unsubscribe_resource",
        "status": "ok",
        "server": op.server,
        "uri": op.uri,
    }


async def handle(op: MCPUnsubscribeResourceIROp, ctx: OpContext) -> dict:
    if ctx.permission_resolver is not None:
        if ctx.intervention_bus is None:
            raise RuntimeError("mcp_unsubscribe_resource op requires intervention_bus on OpContext")
        await ctx.permission_resolver.require_mcp(
            ctx.permission_decl, op.server, ctx.intervention_bus,
            contextual=ctx.contextual_permission,
        )
    return await _execute(op, ctx)


register("mcp_unsubscribe_resource", handle)
