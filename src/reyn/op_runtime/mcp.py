"""mcp kind handler — call a tool on a configured MCP HTTP server."""
from __future__ import annotations
import asyncio
from typing import Literal

from . import register
from .context import OpContext
from ..models import MCPIROp


def _execute(op: MCPIROp, ctx: OpContext) -> dict:
    from ..mcp_client import MCPHTTPClient, MCPError, expand_env

    server_cfg = ctx.mcp_servers.get(op.server)
    if not server_cfg:
        return {
            "kind": "mcp", "status": "error",
            "error": f"MCP server '{op.server}' is not configured. "
                     f"Add it under mcp.servers in reyn.yaml or .reyn/config.yaml.",
        }

    expanded = expand_env(server_cfg)
    url = expanded.get("url", "")
    if not url:
        return {"kind": "mcp", "status": "error",
                "error": f"MCP server '{op.server}' has no url configured."}

    headers = {str(k): str(v) for k, v in (expanded.get("headers") or {}).items()}

    if op.server not in ctx.mcp_clients:
        ctx.mcp_clients[op.server] = MCPHTTPClient(url, headers)
    client = ctx.mcp_clients[op.server]

    ctx.events.emit("mcp_called", server=op.server, tool=op.tool, args=op.args)
    try:
        result = client.call_tool(op.tool, op.args)
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
    return await asyncio.to_thread(_execute, op, ctx)


register("mcp", handle)
