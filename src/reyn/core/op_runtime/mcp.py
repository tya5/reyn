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
    from reyn.mcp.client import MCPClient, MCPError, expand_env

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

    # issue #264 — wire MCP SDK progress + per-call timeout:
    #
    #   progress: forward server-emitted notifications/progress as
    #   ``mcp_progress`` events so the ChatEventForwarder can surface
    #   them in the TUI sticky status (= long-running MCP call
    #   visibility, the A2A PR #253 analogue for the client side).
    #
    #   timeout: per-server ``call_timeout_seconds`` from the raw config
    #   dict; absent → SDK default applies (= no behaviour change for
    #   existing configs that omit the key).
    server_name = op.server
    tool_name = op.tool

    async def _on_progress(
        progress: float, total: float | None, message: str | None,
    ) -> None:
        ctx.events.emit(
            "mcp_progress",
            server=server_name,
            tool=tool_name,
            progress=progress,
            total=total,
            message=message,
        )

    call_timeout = None
    try:
        ct = expanded.get("call_timeout_seconds")
        if ct is not None:
            call_timeout = float(ct)
            if call_timeout <= 0:
                call_timeout = None
    except (TypeError, ValueError):
        call_timeout = None

    ctx.events.emit("mcp_called", server=op.server, tool=op.tool, args=op.args)
    try:
        result = await client.call_tool(
            op.tool, op.args,
            progress_callback=_on_progress,
            timeout_seconds=call_timeout,
        )
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
        # Issue #362: preserve non-text content blocks (images, etc.) so the
        # chat router can forward them to vision-capable models.
        raw_media_blocks = [
            item for item in content_items
            if isinstance(item, dict) and item.get("type") != "text"
        ]
    else:
        text = str(content_items)
        raw_media_blocks = []

    # Issue #383 PR-C: when MediaStore is available, persist image media
    # blocks as flat files under ``.reyn/media/`` and emit path-ref blocks
    # instead of inline base64. Non-image media blocks (= resource, etc.)
    # pass through unchanged for now (= future #385 scope expansion).
    media_blocks: list[dict] = []
    for idx, item in enumerate(raw_media_blocks, start=1):
        if (
            ctx.media_store is not None
            and isinstance(item, dict)
            and item.get("type") == "image"
            and isinstance(item.get("data"), str)
        ):
            import base64
            try:
                raw_bytes = base64.b64decode(item["data"])
            except (ValueError, TypeError):
                # Fall through to legacy inline shape on bad b64
                media_blocks.append(item)
                continue
            mime = item.get("mimeType") or item.get("mime_type") or "image/png"
            media_blocks.append(ctx.media_store.save_image(
                raw_bytes, mime_type=mime,
                chain_id=ctx.run_id or "",
                tool=f"mcp_{op.server}_{op.tool}",
                seq=idx,
            ))
        else:
            media_blocks.append(item)

    is_error = bool(result.get("isError"))
    ctx.events.emit(
        "mcp_completed", server=op.server, tool=op.tool, is_error=is_error,
        media_block_count=len(media_blocks),
    )
    return {
        "kind": "mcp",
        "status": "error" if is_error else "ok",
        "server": op.server,
        "tool": op.tool,
        "content": text,
        "media_blocks": media_blocks,
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
