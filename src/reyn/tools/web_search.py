"""web_search ToolDefinition — POC migration (ADR-0026 M2).

This is the FIRST capability migrated to the unified registry.
The existing handler in src/reyn/op_runtime/web.py is preserved
and wrapped via a thin adapter that translates between the old
(op, ctx, caller) signature and the new (args, ctx) signature.

Both router-style and phase-style dispatch paths consume this
ToolDefinition; M2 verifies byte-identity for both surfaces.
"""
from __future__ import annotations

from typing import Any, Mapping

from reyn.tools.types import ToolContext, ToolDefinition, ToolGates, ToolResult

# Description must be byte-identical to the current router_tools.py
# ToolSpec.description for web_search (= the operator-hint extended
# string from commit 8af3444). Copied verbatim.
_WEB_SEARCH_DESCRIPTION = (
    "Search the public web with DuckDuckGo and return "
    "structured results. Standard search operators are "
    "supported in `query`: `site:<domain>` to scope to "
    "one site (e.g. `site:news.ycombinator.com`), "
    "`\"phrase\"` for exact match, `-term` to exclude. "
    "Use them when the user's intent is site-specific "
    "or phrase-anchored; plain keywords work otherwise. "
    "query: search string. "
    "max_results: cap on returned results (default 5)."
)

# Parameters JSON schema must be byte-identical to the current
# router_tools.py ToolSpec.parameters for web_search.
_WEB_SEARCH_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "query": {"type": "string"},
        "max_results": {"type": "integer"},
    },
    "required": ["query"],
}


async def _handle(args: Mapping[str, Any], ctx: ToolContext) -> ToolResult:
    """Adapter wrapping op_runtime.web.handle_web_search.

    Bridges between the unified (args, ctx) signature and the
    existing (op, ctx, caller) signature. Once M2 succeeds, the
    body of handle_web_search may be inlined here in M4 cleanup.
    """
    # Lazy import to avoid circular dependency at registry-init time.
    from reyn.op_runtime.context import OpContext
    from reyn.op_runtime.web import handle_web_search
    from reyn.permissions.permissions import PermissionDecl
    from reyn.schemas.models import WebSearchIROp

    # Build a transient WebSearchIROp from args (= reuse Pydantic
    # validation that the existing op handler expects).
    op = WebSearchIROp(
        kind="web_search",
        query=args["query"],
        max_results=int(args.get("max_results", 5)),
    )

    # Build a legacy OpContext from the new ToolContext.
    # Propagate the active phase's PermissionDecl via
    # phase_state.op_context (FP-0008 Tool→OpContext bridge fix
    # 2026-05-28). Web search is read-only / public queries today, but
    # uniform bridge wiring avoids future class bugs if web_search
    # gains permission-gated paths.
    #
    # events.subscribers: the existing OpContext constructor requires
    # this to forward subscribers to sub-skill invocations. Web search
    # does not spawn sub-skills, but OpContext.subscribers is set
    # defensively via getattr fallback.
    phase_op_ctx = (
        ctx.phase_state.op_context if ctx.phase_state is not None else None
    )
    legacy_ctx = OpContext(
        workspace=ctx.workspace,
        events=ctx.events,
        permission_decl=(
            phase_op_ctx.permission_decl
            if phase_op_ctx is not None
            else PermissionDecl()
        ),
        permission_resolver=ctx.permission_resolver,
        skill_name="",
        skill=None,
        model="standard",
        resolver=None,
        subscribers=getattr(ctx.events, "subscribers", []),
        output_language=None,
        max_phase_visits=25,
        sub_state_dir_override=None,
        state_dir_strategy="control_ir",
        shell_allowed=False,
        mcp_servers={},
        mcp_clients={},
        intervention_bus=None,
        current_phase="",
        caller="direct",
        parent_skill_run_id=None,
    )

    return await handle_web_search(op=op, ctx=legacy_ctx, caller="control_ir")


WEB_SEARCH = ToolDefinition(
    name="web_search",
    description=_WEB_SEARCH_DESCRIPTION,
    parameters=_WEB_SEARCH_PARAMETERS,
    gates=ToolGates(router="allow", phase="allow"),
    handler=_handle,
    category="discovery",
    purity="read_only",   # web search has no side effect on workspace
)
