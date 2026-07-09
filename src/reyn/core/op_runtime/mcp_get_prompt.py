"""mcp_get_prompt kind handler — fetch one rendered prompt on a configured MCP server.

#2597 slice ②c (prompts consumption). Mirrors ``op_runtime/mcp_read_resource.py``'s
``mcp_read_resource`` handler exactly: same server-config resolution, same
``MCPGateway`` seam (fault containment + task-affine pool reuse + per-call
timeout), same ``require_mcp`` permission gate (server-scoped — the same
axis a tool call / resource read uses, since a rendered prompt is external,
possibly sensitive server-authored content, just like a resource read), and
the same before/after event pair shape (``mcp_prompt_get`` / a
completed-or-failed follow-up — mirrors ``mcp_resource_read``/
``mcp_resource_read_completed``/``mcp_resource_read_failed``).

Discovery (``list_prompts``) is deliberately NOT an op kind here — it mirrors
``list_resources``/``list_tools``, which bypass the permission gate + op-kind
machinery entirely (see ``session.py::_mcp_list_prompts``). Only the
content-returning get is gated, matching the tools/resources surface's own
split between ungated `list_*` and gated `call_tool`/`read_resource`.

Prompts have no subscribe concept — out of scope entirely, unlike resources.
"""
from __future__ import annotations

from reyn.schemas.models import MCPGetPromptIROp

from . import register
from .context import OpContext


async def _execute(op: MCPGetPromptIROp, ctx: OpContext) -> dict:
    from reyn.mcp.client import expand_env
    from reyn.mcp.gateway import MCPFault, MCPGateway

    server_cfg = ctx.mcp_servers.get(op.server)
    if not server_cfg:
        return {
            "kind": "mcp_get_prompt", "status": "error",
            "error": f"MCP server '{op.server}' is not configured. "
                     f"Add it under mcp.servers in reyn.yaml or reyn.local.yaml.",
        }

    expanded = expand_env(server_cfg)
    if not isinstance(expanded, dict):
        return {"kind": "mcp_get_prompt", "status": "error",
                "error": f"MCP server '{op.server}' config must be a dict."}

    if "type" not in expanded and expanded.get("url"):
        expanded = {**expanded, "type": "http"}

    if ctx.mcp_connection_service is None and ctx.mcp_pool is None:
        return {"kind": "mcp_get_prompt", "status": "error", "server": op.server,
                "name": op.name, "error": "no MCP client pool on this context"}

    ctx.events.emit("mcp_prompt_get", server=op.server, name=op.name)
    gateway = MCPGateway(pool=ctx.mcp_connection_service or ctx.mcp_pool, agent_id=ctx.agent_id)
    try:
        result = await gateway.get_prompt(op.server, op.name, op.arguments, expanded)
    except MCPFault as fault_exc:
        fault = str(fault_exc)
        ctx.events.emit("mcp_prompt_get_failed", server=op.server, name=op.name, error=fault)
        return {"kind": "mcp_get_prompt", "status": "error", "server": op.server,
                "name": op.name, "error": fault}

    messages = result.get("messages", [])
    ctx.events.emit(
        "mcp_prompt_get_completed", server=op.server, name=op.name,
        message_count=len(messages) if isinstance(messages, list) else 0,
    )
    return {
        "kind": "mcp_get_prompt",
        "status": "ok",
        "server": op.server,
        "name": op.name,
        "description": result.get("description"),
        "messages": messages,
    }


async def handle(op: MCPGetPromptIROp, ctx: OpContext) -> dict:
    if ctx.permission_resolver is not None:
        if ctx.intervention_bus is None:
            raise RuntimeError("mcp_get_prompt op requires intervention_bus on OpContext")
        await ctx.permission_resolver.require_mcp(
            ctx.permission_decl, op.server, ctx.intervention_bus,
            contextual=ctx.contextual_permission,
        )
    return await _execute(op, ctx)


from reyn.core.offload.canonical import mcp_get_prompt_to_canonical  # noqa: E402

register("mcp_get_prompt", handle, canonical=mcp_get_prompt_to_canonical)
