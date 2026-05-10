"""recall ToolDefinition (ADR-0033 Phase 1).

RECALL is both router-callable and phase-callable (gates.router=allow,
gates.phase=allow). It is the primary LLM entry point for semantic search
over indexed sources.

The handler delegates to op_runtime.recall.handle, which orchestrates:
  1. embed sub-op (query → vector)
  2. index_query sub-op for each source
  3. merge + top-K ranking

Per ADR-0026: the ToolDefinition lives here; registration is in
get_default_registry() in tools/__init__.py.
"""
from __future__ import annotations

from typing import Any, Mapping

from reyn.tools.types import ToolContext, ToolDefinition, ToolGates, ToolResult

# B22 schema-layer fix: strengthen affordance signal with concrete use-case
# enumeration so natural concept questions ("what is X?", "explain X")
# route here when an indexed source covers the topic — without coupling
# the description to specific source names (= per A4 constraint, the SP
# carries the source list, this description must be source-agnostic).
_RECALL_DESCRIPTION = (
    "Search indexed sources by natural-language query. Returns top-K "
    "relevant chunks with text + metadata. Use this when the user's "
    "question is about a topic an indexed source covers — including "
    "'what is X?', 'explain X', 'how does X work?' style questions. "
    "Pick sources from the 'Indexed sources' section in the system "
    "prompt; each source's description tells you what topics it covers. "
    "Prefer this over `reyn_src_read` / file_read when an indexed source "
    "description matches the question's topic — semantic search across "
    "indexed chunks is more reliable than guessing a file path."
)

_RECALL_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": "Natural language query to search for.",
        },
        "sources": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Logical source names to search (from Indexed sources list)."
            ),
        },
        "top_k": {
            "type": "integer",
            "default": 5,
            "minimum": 1,
            "maximum": 50,
            "description": "Number of top chunks to return.",
        },
        "filters": {
            "type": "object",
            "default": {},
            "description": (
                "ChunkMetadata field equality filters (e.g. source_path)."
            ),
        },
        "embedding_model": {
            "type": "string",
            "default": "standard",
            "description": (
                "Embedding model class (light/standard/strong) or full model id."
            ),
        },
    },
    "required": ["query", "sources"],
}


async def _handle_recall(args: Mapping[str, Any], ctx: ToolContext) -> ToolResult:
    """Dispatch the recall macro op via op_runtime.

    Builds a RecallIROp from args and calls the registered recall handler.
    OpContext is obtained from ctx.phase_state.op_context when available
    (phase-side call) or constructed minimally (router-side call).
    """
    from reyn.op_runtime import execute_op
    from reyn.op_runtime.context import OpContext
    from reyn.permissions.permissions import PermissionDecl
    from reyn.schemas.models import RecallIROp

    op = RecallIROp(
        kind="recall",
        query=str(args["query"]),
        sources=list(args["sources"]),
        top_k=int(args.get("top_k", 5)),
        filters=dict(args.get("filters") or {}),
        embedding_model=str(args.get("embedding_model", "standard")),
    )

    # Obtain or build OpContext from ToolContext.
    _op_ctx = (
        ctx.phase_state.op_context
        if ctx.phase_state is not None
        else None
    )
    if _op_ctx is not None and isinstance(_op_ctx, OpContext):
        legacy_ctx = _op_ctx
    elif (
        ctx.router_state is not None
        and ctx.router_state.op_context_factory is not None
    ):
        legacy_ctx = ctx.router_state.op_context_factory()
    else:
        # Minimal context for router-side calls without a factory.
        # Recall is read-only with respect to the workspace (no writes).
        legacy_ctx = OpContext(
            workspace=ctx.workspace,
            events=ctx.events,
            permission_decl=PermissionDecl(),
            permission_resolver=ctx.permission_resolver,
            skill_name="",
            subscribers=getattr(ctx.events, "subscribers", []),
        )

    return await execute_op(op, legacy_ctx, caller="control_ir")


RECALL = ToolDefinition(
    name="recall",
    description=_RECALL_DESCRIPTION,
    parameters=_RECALL_PARAMETERS,
    gates=ToolGates(router="allow", phase="allow"),
    handler=_handle_recall,
    category="discovery",
    purity="read_only",
)
