"""mcp_read_resource kind handler — read one resource on a configured MCP server.

#2597 slice ②a (resources consumption). Mirrors ``op_runtime/mcp.py``'s
``mcp`` (call_tool) handler exactly: same server-config resolution, same
``MCPGateway`` seam (fault containment + task-affine pool reuse + per-call
timeout), same ``require_mcp`` permission gate (server-scoped — the same
axis a tool call uses, since a resource read is external, possibly
sensitive server-authored content, just like a tool result), and the same
before/after event pair shape (``mcp_resource_read`` / a completed-or-failed
follow-up — mirrors ``mcp_called``/``mcp_completed``/``mcp_failed``).

Discovery (``list_resources``/``list_resource_templates``) is deliberately
NOT an op kind here — it mirrors ``list_tools``, which bypasses the
permission gate + op-kind machinery entirely (see
``session.py::_mcp_list_resources``). Only the content-returning read is
gated, matching the tools surface's own split between ungated `list_tools`
and gated `call_tool`.

Subscribe / resources/updated / on_resource_updated (slice ②b) are OUT OF
SCOPE here.
"""
from __future__ import annotations

from reyn.schemas.models import MCPReadResourceIROp

from . import register
from .context import OpContext


async def _execute(op: MCPReadResourceIROp, ctx: OpContext) -> dict:
    from reyn.core.cancellable import Cancelled
    from reyn.mcp.client import expand_env
    from reyn.mcp.gateway import MCPFault, MCPGateway

    server_cfg = ctx.mcp_servers.get(op.server)
    if not server_cfg:
        return {
            "kind": "mcp_read_resource", "status": "error",
            "error": f"MCP server '{op.server}' is not configured. "
                     f"Add it under mcp.servers in reyn.yaml or reyn.local.yaml.",
        }

    expanded = expand_env(server_cfg)
    if not isinstance(expanded, dict):
        return {"kind": "mcp_read_resource", "status": "error",
                "error": f"MCP server '{op.server}' config must be a dict."}

    if "type" not in expanded and expanded.get("url"):
        expanded = {**expanded, "type": "http"}

    if ctx.mcp_connection_service is None and ctx.mcp_pool is None:
        return {"kind": "mcp_read_resource", "status": "error", "server": op.server,
                "uri": op.uri, "error": "no MCP client pool on this context"}

    ctx.events.emit("mcp_resource_read", server=op.server, uri=op.uri)
    gateway = MCPGateway(
        pool=ctx.mcp_connection_service or ctx.mcp_pool, agent_id=ctx.agent_id,
        cancel_event=ctx.cancel_event,
    )
    try:
        result = await gateway.read_resource(op.server, op.uri, expanded)
    except Cancelled:
        ctx.events.emit("mcp_resource_read_cancelled", server=op.server, uri=op.uri)
        return {"kind": "mcp_read_resource", "status": "cancelled", "server": op.server, "uri": op.uri}
    except MCPFault as fault_exc:
        fault = str(fault_exc)
        ctx.events.emit("mcp_resource_read_failed", server=op.server, uri=op.uri, error=fault)
        return {"kind": "mcp_read_resource", "status": "error", "server": op.server,
                "uri": op.uri, "error": fault}

    contents = result.get("contents", [])
    ctx.events.emit(
        "mcp_resource_read_completed", server=op.server, uri=op.uri,
        content_count=len(contents) if isinstance(contents, list) else 0,
    )
    return {
        "kind": "mcp_read_resource",
        "status": "ok",
        "server": op.server,
        "uri": op.uri,
        "contents": contents,
    }


async def handle(op: MCPReadResourceIROp, ctx: OpContext) -> dict:
    if ctx.permission_resolver is not None:
        if ctx.intervention_bus is None:
            raise RuntimeError("mcp_read_resource op requires intervention_bus on OpContext")
        await ctx.permission_resolver.require_mcp(
            ctx.permission_decl, op.server, ctx.intervention_bus,
            contextual=ctx.contextual_permission,
        )
    return await _execute(op, ctx)


from reyn.core.offload.canonical import mcp_read_resource_to_canonical  # noqa: E402

register("mcp_read_resource", handle, canonical=mcp_read_resource_to_canonical)
