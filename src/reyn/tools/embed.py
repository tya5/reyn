"""embed ToolDefinition (FP-0057 Phase 1).

`embed` is the raw, USER-FACING embedding primitive: a batch of texts in,
a batch of vectors out. The user composes `embed` -> their own external MCP
vector-DB's store/retrieve tools via pipeline (reyn never hosts a user RAG
store, per the FP-0057 design). Default-ALLOW (compute op — cost is the
embedding API/compute, not a workspace side effect); individually
name-gateable via `contextual_gate` like every other op kind.

ADDITIVE at Phase 1: this did NOT retire `embed_and_index` (the CodeAct-only
ingestion entry) — that clean-break landed in FP-0057 Phase 2b, which
replaced it with `reyn.api.safe.index_update` (a thin dispatch onto the
`index_update` op below).

Per ADR-0026: the ToolDefinition lives here; registration is in
get_default_registry() in tools/__init__.py.
"""
from __future__ import annotations

from typing import Any, Mapping

from reyn.core.offload.canonical import embed_to_canonical
from reyn.tools.types import ToolContext, ToolDefinition, ToolGates, ToolResult

_EMBED_DESCRIPTION = (
    "Embed a batch of texts into vectors using reyn's configured embedding "
    "model (raw primitive — no storage). Returns one vector per input text, "
    "in the same order. Use this to build your OWN persistent RAG store: "
    "embed your texts, then hand the vectors to your own vector-DB MCP "
    "tools (store/upsert) to persist them, and again at query time to embed "
    "a query before calling that store's search tool. For reyn's OWN "
    "indexed sources / memory / tool-use retrieval, use `recall` instead — "
    "it embeds and searches in one call over reyn-managed indexes."
)

_EMBED_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "texts": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Texts to embed. Returned vectors preserve this order.",
        },
        "embedding_model": {
            "type": "string",
            "default": "standard",
            "description": (
                "Embedding model class (light/standard/strong) or a full "
                "provider model id."
            ),
        },
    },
    "required": ["texts"],
}


async def _handle_embed(args: Mapping[str, Any], ctx: ToolContext) -> ToolResult:
    """Dispatch an EmbedIROp via op_runtime (mirrors tools/recall.py's shape)."""
    from reyn.core.op_runtime import execute_op
    from reyn.core.op_runtime.context import OpContext
    from reyn.schemas.models import EmbedIROp
    from reyn.security.permissions.permissions import PermissionDecl

    texts = args.get("texts")
    if not texts:
        return {
            "ok": False,
            "error_kind": "missing_required_arg",
            "error_message": "embed requires a non-empty `texts` array.",
            "missing": ["texts"],
        }

    op = EmbedIROp(
        kind="embed",
        texts=[str(t) for t in texts],
        embedding_model=str(args.get("embedding_model", "standard")),
    )

    if (
        ctx.router_state is not None
        and ctx.router_state.op_context_factory is not None
    ):
        legacy_ctx = ctx.router_state.op_context_factory()
    else:
        # Minimal context for router-side calls without a factory. embed has
        # no workspace side effect (read-only w.r.t. the workspace; its only
        # effect is the outbound embedding API call), so a workspace-less
        # OpContext is safe here — same posture as tools/recall.py.
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


EMBED = ToolDefinition(
    canonical=embed_to_canonical,
    name="embed",
    router_dispatched=True,
    description=_EMBED_DESCRIPTION,
    parameters=_EMBED_PARAMETERS,
    gates=ToolGates(router="allow", phase="allow"),
    handler=_handle_embed,
    category="discovery",
    purity="read_only",
)
