"""semantic_search ToolDefinition (ADR-0033 Phase 1; FP-0057 Phase 2a renamed
from `recall` — clean break, fixes the observed
`recall`/`search_actions`/`memory` naming collision).

SEMANTIC_SEARCH is both router-callable and phase-callable (gates.router=allow,
gates.phase=allow). It is the primary LLM entry point for semantic search
over indexed sources.

The handler delegates to op_runtime.semantic_search.handle, which
orchestrates:
  1. per-source-model resolution (auto-adopt from SourceManifest / index stat)
  2. one embed call per DISTINCT model group (never once-for-all — the
     multi-model correctness fix, co-vet #1)
  3. index_query sub-op for each source, with its group's matching vector
  4. merge WITHIN each model group by score; combine ACROSS groups by
     round-robin (never comparing cross-model score magnitudes)

Per ADR-0026: the ToolDefinition lives here; registration is in
get_default_registry() in tools/__init__.py.
"""
from __future__ import annotations

from typing import Any, Mapping

from reyn.tools.descriptions import discovery
from reyn.tools.types import ToolContext, ToolDefinition, ToolGates, ToolResult

# B22 schema-layer fix: strengthen affordance signal with concrete use-case
# enumeration so natural concept questions ("what is X?", "explain X")
# route here when an indexed source covers the topic — without coupling
# the description to specific source names (= per A4 constraint, the SP
# carries the source list, this description must be source-agnostic).
#
# Reviewable in src/reyn/tools/descriptions/discovery.py (Phase 1 of the
# tool-description package refactor) — these aliases keep the call sites
# unchanged (byte-identical relocation, no LLM-facing text change).
#
# B23-PRE-1 SP role-separation note: semantic_search vs memory disambiguation
# that previously lived in the SP disambiguation block is now in
# _SEMANTIC_SEARCH_DESCRIPTION_HIDE_LEGACY below. _SEMANTIC_SEARCH_DESCRIPTION
# remains byte-identical to the pre-rename _RECALL_DESCRIPTION text to
# preserve LLMReplay fixture stability.
_SEMANTIC_SEARCH_DESCRIPTION = discovery.semantic_search.text

# B23-PRE-1 SP role-separation: enriched WHAT/WHEN/WHEN_NOT variant for
# wrapper-only path. Carries the semantic_search vs memory disambiguation that
# previously lived in the SP disambiguation block. _SEMANTIC_SEARCH_DESCRIPTION
# (above) stays byte-identical for LLMReplay fixture stability.
# Tests check this constant; describe_action in wrapper mode can expose it.
_SEMANTIC_SEARCH_DESCRIPTION_HIDE_LEGACY = discovery.semantic_search_hide_legacy.text

_SEMANTIC_SEARCH_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": discovery.PARAMS["semantic_search"]["query"].text,
        },
        "sources": {
            "type": "array",
            "items": {"type": "string"},
            "description": discovery.PARAMS["semantic_search"]["sources"].text,
        },
        "top_k": {
            "type": "integer",
            "default": 5,
            "minimum": 1,
            "maximum": 50,
            "description": discovery.PARAMS["semantic_search"]["top_k"].text,
        },
        "filters": {
            "type": "object",
            "default": {},
            "description": discovery.PARAMS["semantic_search"]["filters"].text,
        },
        "embedding_model": {
            "type": "string",
            "default": "standard",
            "description": discovery.PARAMS["semantic_search"]["embedding_model"].text,
        },
    },
    "required": ["query", "sources"],
}


async def _handle_semantic_search(args: Mapping[str, Any], ctx: ToolContext) -> ToolResult:
    """Dispatch the semantic_search macro op via op_runtime.

    Builds a SemanticSearchIROp from args and calls the registered
    semantic_search handler. OpContext is obtained from
    ctx.router_state.op_context_factory when available, or constructed
    minimally otherwise.
    """
    from reyn.core.op_runtime import execute_op
    from reyn.core.op_runtime.context import OpContext
    from reyn.schemas.models import SemanticSearchIROp
    from reyn.security.permissions.permissions import PermissionDecl

    # Defensive arg validation. LLMs sometimes call `semantic_search` without
    # the required keys (= when no "Indexed sources" appears in the
    # system prompt, or when the LLM forgets the schema). Raising a
    # raw KeyError leaks the literal Python exception into the
    # tool_failed event and the user-facing reply (observed in
    # dogfood B45/B46 W3 `recall_indexed_source` scenario, pre-rename). Return
    # a structured tool result instead so the LLM can compose a clean
    # "no indexed sources" reply.
    missing = [k for k in ("query", "sources") if not args.get(k)]
    if missing:
        return {
            "ok": False,
            "error_kind": "missing_required_arg",
            "error_message": (
                f"semantic_search requires {missing}. "
                "Available sources are listed under 'Indexed sources' in "
                "the system prompt; if none are listed, no sources have "
                "been indexed yet for this agent."
            ),
            "missing": missing,
        }

    op = SemanticSearchIROp(
        kind="semantic_search",
        query=str(args["query"]),
        sources=list(args["sources"]),
        top_k=int(args.get("top_k", 5)),
        filters=dict(args.get("filters") or {}),
        embedding_model=str(args.get("embedding_model", "standard")),
    )

    # Obtain or build OpContext from ToolContext.
    if (
        ctx.router_state is not None
        and ctx.router_state.op_context_factory is not None
    ):
        legacy_ctx = ctx.router_state.op_context_factory()
    else:
        # Minimal context for router-side calls without a factory.
        # semantic_search is read-only with respect to the workspace (no writes).
        legacy_ctx = OpContext(
            workspace=ctx.workspace,
            events=ctx.events,
            permission_decl=PermissionDecl(),
            permission_resolver=ctx.permission_resolver,
            actor="",
            subscribers=getattr(ctx.events, "subscribers", []),
            # #1673: thread the config-aware resolver so this OpContext is never
            # resolver=None (the bug-class invariant). semantic_search uses
            # per-source auto-adopted embedding_model for its embedding calls
            # — no chat-LLM sink here — but the uniform threading keeps the
            # "no tool OpContext is resolver=None" invariant.
            resolver=ctx.resolver,
        )

    return await execute_op(op, legacy_ctx)


from reyn.core.offload.canonical import chunks_to_canonical  # noqa: E402

SEMANTIC_SEARCH = ToolDefinition(
    canonical=chunks_to_canonical,
    name="semantic_search",
    router_dispatched=True,
    description=_SEMANTIC_SEARCH_DESCRIPTION,
    parameters=_SEMANTIC_SEARCH_PARAMETERS,
    gates=ToolGates(router="allow", phase="allow"),
    handler=_handle_semantic_search,
    category="discovery",
    purity="read_only",
    returns_external_content=True,  # FP-0050/#1822: RAG over user content (memory/docs/chat)
)
