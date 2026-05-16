"""mcp kind handler — call a tool on a configured MCP server.

Supports stdio + Streamable HTTP transports (sse deferred). The transport
is selected per-server via the ``type:`` field in ``mcp.servers.<name>``;
configs that omit ``type`` default to ``http`` for backward compatibility
with pre-PR32 reyn.yaml files.
"""
from __future__ import annotations

from typing import Literal

from reyn.schemas.models import MCPIROp

from . import register
from .context import OpContext


async def _execute(op: MCPIROp, ctx: OpContext) -> dict:
    from reyn.mcp_client import MCPClient, MCPError, expand_env

    server_cfg = ctx.mcp_servers.get(op.server)
    if not server_cfg:
        return {
            "kind": "mcp", "status": "error",
            "error": f"MCP server '{op.server}' is not configured. "
                     f"Add it under mcp.servers in reyn.yaml or reyn.local.yaml.",
        }

    expanded = expand_env(server_cfg)
    if not isinstance(expanded, dict):
        return {"kind": "mcp", "status": "error",
                "error": f"MCP server '{op.server}' config must be a dict."}

    # Backward compat: a config with `url` but no `type` is treated as http.
    if "type" not in expanded:
        if expanded.get("url"):
            expanded = {**expanded, "type": "http"}

    if op.server not in ctx.mcp_clients:
        try:
            # FP-0016 E: thread agent_id so X-Reyn-Agent-Id is added to
            # outgoing MCP HTTP requests.
            ctx.mcp_clients[op.server] = MCPClient(expanded, agent_id=ctx.agent_id)
        except ValueError as exc:
            return {"kind": "mcp", "status": "error", "server": op.server,
                    "tool": op.tool, "error": str(exc)}
    client = ctx.mcp_clients[op.server]

    ctx.events.emit("mcp_called", server=op.server, tool=op.tool, args=op.args)
    try:
        result = await client.call_tool(op.tool, op.args)
    except MCPError as exc:
        ctx.events.emit("mcp_failed", server=op.server, tool=op.tool, error=str(exc))
        return {"kind": "mcp", "status": "error", "server": op.server,
                "tool": op.tool, "error": str(exc)}

    content_items = result.get("content", [])
    if isinstance(content_items, list):
        text = "\n".join(
            item.get("text", "") for item in content_items
            if isinstance(item, dict) and item.get("type") == "text"
        )
    else:
        text = str(content_items)

    is_error = bool(result.get("isError"))
    ctx.events.emit("mcp_completed", server=op.server, tool=op.tool, is_error=is_error)
    return {
        "kind": "mcp",
        "status": "error" if is_error else "ok",
        "server": op.server,
        "tool": op.tool,
        "content": text,
        "raw": result,
    }


async def handle(op: MCPIROp, ctx: OpContext, caller: Literal["preprocessor", "control_ir"]) -> dict:
    if ctx.permission_resolver is not None:
        if ctx.intervention_bus is None:
            raise RuntimeError("mcp op requires intervention_bus on OpContext")
        await ctx.permission_resolver.require_mcp(
            ctx.permission_decl, op.server, ctx.intervention_bus,
        )
    return await _execute(op, ctx)


register("mcp", handle)
