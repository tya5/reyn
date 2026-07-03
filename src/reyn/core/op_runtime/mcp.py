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

# #2421: the per-call MCP timeout ([4]) now lives in the MCPGateway seam (``resolve_call_timeout``),
# applied to every MCP op in one place. The op handler delegates to the gateway.


async def _execute(op: MCPIROp, ctx: OpContext) -> dict:
    from reyn.mcp.client import expand_env
    from reyn.mcp.gateway import MCPFault, MCPGateway

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

    # #a359 P2: the client comes from the per-turn structured pool (opened + closed in the pool's
    # owning task — no cross-SDK-task teardown). None = no pool wired on this ctx (a non-MCP
    # OpContext should never reach the mcp handler; guard defensively).
    if ctx.mcp_pool is None:
        return {"kind": "mcp", "status": "error", "server": op.server,
                "tool": op.tool, "error": "no MCP client pool on this context"}

    # issue #264 — wire MCP SDK progress + per-call timeout:
    #
    #   progress: forward server-emitted notifications/progress as
    #   ``mcp_progress`` events on the run's EventLog so a subscriber can
    #   surface them in the TUI sticky status (= long-running MCP call
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

    ctx.events.emit("mcp_called", server=op.server, tool=op.tool, args=op.args)
    # #2421: the open+call+teardown fault boundary + per-call timeout + task-affine lifecycle all live
    # in the ONE MCPGateway seam (reusing this turn's pool). The gateway raises only MCPFault (an
    # Exception) or genuine control flow — never a bare BaseExceptionGroup — so a server that dies on
    # connect, a bad config, a malformed response, or a transport group all surface here as a clean
    # MCPFault → contained error tool-result (owner req: MCP misbehavior must not crash the router
    # loop). Cancellation is never swallowed (is_real_control_flow re-raises genuine cancel/KI/SE).
    gateway = MCPGateway(pool=ctx.mcp_pool, agent_id=ctx.agent_id)
    try:
        result = await gateway.call_tool(
            op.server, op.tool, op.args, expanded, progress_cb=_on_progress,
        )
    except MCPFault as fault_exc:
        # Owner req: feed the fault CONTENT back to the LLM (type + message, group members
        # aggregated) via the standard op-error result — so it can retry/adapt, not a silent error.
        fault = str(fault_exc)
        ctx.events.emit("mcp_failed", server=op.server, tool=op.tool, error=fault)
        return {"kind": "mcp", "status": "error", "server": op.server,
                "tool": op.tool, "error": fault}

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
    out = {
        "kind": "mcp",
        "status": "error" if is_error else "ok",
        "server": op.server,
        "tool": op.tool,
        "content": text,
        "media_blocks": media_blocks,
        # #2336: declare the joined text as the offload payload so an oversized result is stored
        # CLEAN (real newlines, not a whole-dict single-line JSON envelope). This ONLY fires when
        # ``content`` is the SOLE oversized field. We dropped the former ``raw`` (the full flattened
        # CallToolResult): it re-carried the same oversized ``content``, so both went oversized →
        # gate missed → whole-dict fallback (the bug). ``isError`` is already surfaced as ``status``
        # and the joined text is ``content``; ``structuredContent`` is the only non-duplicate SDK
        # field, so it is preserved as ``structured`` below when present (never re-carries ``content``,
        # so it stays a legitimate — and usually absent — second field).
        "_offload_payload_field": "content",
    }
    # Preserve a real MCP structured-output only when the tool actually returned one (None by
    # default) — absent → no field (clean end-state, no shim); present → the LLM keeps the data.
    structured = result.get("structuredContent")
    if structured is not None:
        out["structured"] = structured
    return out


async def handle(op: MCPIROp, ctx: OpContext) -> dict:
    if ctx.permission_resolver is not None:
        if ctx.intervention_bus is None:
            raise RuntimeError("mcp op requires intervention_bus on OpContext")
        await ctx.permission_resolver.require_mcp(
            ctx.permission_decl, op.server, ctx.intervention_bus,
            contextual=ctx.contextual_permission,  # #2074 S4a/S4b (OpContext field)
        )
    return await _execute(op, ctx)


register("mcp", handle)
