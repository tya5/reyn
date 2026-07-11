"""index_update ToolDefinition (FP-0057 Phase 2a).

Incremental / delta-reconcile ingestion into a source's in-core
`IndexBackend` — NO full-rebuild mode (a from-scratch rebuild is
`index_drop` -> `index_update` on an empty source). The handler delegates to
`op_runtime.index_update.handle`, which reconciles the caller-supplied
`chunks` against the source's current index (add / update / remove / skip
by `content_hash` within each `source_path`) and embeds only the
add/update delta via the shared `embed` op (no duplicated embed logic).

Default-ALLOW (own-write op — writes only to the source's OWN index +
manifest, not a destructive cross-cutting op like `index_drop`);
individually name-gateable via `contextual_gate`.

Per ADR-0026: the ToolDefinition lives here; registration is in
get_default_registry() in tools/__init__.py.
"""
from __future__ import annotations

from typing import Any, Mapping

from reyn.core.offload.canonical import index_update_to_canonical
from reyn.tools.types import ToolContext, ToolDefinition, ToolGates, ToolResult

_INDEX_UPDATE_DESCRIPTION = (
    "Incrementally ingest chunks into an indexed source, reconciling "
    "against its current content: NEW content_hash values are embedded and "
    "added; a source_path whose chunks changed (new hash under a path "
    "already indexed) is updated (old chunks for that path replaced); a "
    "source_path this call re-supplies chunks for but whose old chunk "
    "hashes are absent from this call are removed; unchanged content_hash "
    "values are skipped (no re-embed). NO full-rebuild mode — to rebuild a "
    "source from scratch, call `drop_source` then `index_update` on the "
    "fresh (empty) source. The caller supplies pre-chunked text (chunking "
    "is the caller's responsibility, not this tool's)."
)

_INDEX_UPDATE_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "source": {
            "type": "string",
            "description": "Logical source name to ingest into.",
        },
        "chunks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "metadata": {
                        "type": "object",
                        "description": (
                            "content_hash (required, change-detection key), "
                            "source_path (required, reconciliation-scope "
                            "key), plus optional source_type / chunk_index "
                            "/ size_tokens / parent_context / extra."
                        ),
                    },
                },
                "required": ["text", "metadata"],
            },
            "description": "Chunks to reconcile into the index.",
        },
        "embedding_model": {
            "type": "string",
            "default": "standard",
            "description": (
                "Embedding model class, used ONLY when this source has no "
                "recorded model yet (first index_update for a new source) "
                "— an already-indexed source's recorded model always wins "
                "(a source is one embedding space)."
            ),
        },
        "description": {
            "type": "string",
            "description": "SourceManifest description (first-index or override).",
        },
        "path": {
            "type": "string",
            "description": "SourceManifest path label (first-index or override).",
        },
    },
    "required": ["source", "chunks"],
}


async def _handle_index_update(args: Mapping[str, Any], ctx: ToolContext) -> ToolResult:
    """Dispatch an IndexUpdateIROp via op_runtime (mirrors tools/semantic_search.py's shape)."""
    from reyn.core.op_runtime import execute_op
    from reyn.core.op_runtime.context import OpContext
    from reyn.schemas.models import IndexUpdateIROp
    from reyn.security.permissions.permissions import PermissionDecl

    missing = [k for k in ("source", "chunks") if not args.get(k)]
    if missing:
        return {
            "ok": False,
            "error_kind": "missing_required_arg",
            "error_message": f"index_update requires {missing}.",
            "missing": missing,
        }

    op = IndexUpdateIROp(
        kind="index_update",
        source=str(args["source"]),
        chunks=[dict(c) for c in args["chunks"]],
        embedding_model=str(args.get("embedding_model", "standard")),
        description=args.get("description"),
        path=args.get("path"),
    )

    if (
        ctx.router_state is not None
        and ctx.router_state.op_context_factory is not None
    ):
        legacy_ctx = ctx.router_state.op_context_factory()
    else:
        legacy_ctx = OpContext(
            workspace=ctx.workspace,
            events=ctx.events,
            permission_decl=PermissionDecl(),
            permission_resolver=ctx.permission_resolver,
            actor="",
            subscribers=getattr(ctx.events, "subscribers", []),
            resolver=ctx.resolver,
        )

    return await execute_op(op, legacy_ctx)


INDEX_UPDATE = ToolDefinition(
    canonical=index_update_to_canonical,
    name="index_update",
    router_dispatched=True,
    description=_INDEX_UPDATE_DESCRIPTION,
    parameters=_INDEX_UPDATE_PARAMETERS,
    gates=ToolGates(router="allow", phase="allow"),
    handler=_handle_index_update,
    category="discovery",
    purity="side_effect",
)
