"""web_fetch ToolDefinition — Wave 1 migration (ADR-0026 M3).

Mirrors web_search.py structure. The existing handler in
src/reyn/op_runtime/web.py is preserved and wrapped via a thin
adapter that translates between the old (op, ctx, caller) signature
and the new (args, ctx) signature.

Both router-style and phase-style dispatch paths consume this
ToolDefinition; Wave 1 verifies byte-identity for both surfaces.
"""
from __future__ import annotations

from typing import Any, Mapping

from reyn.tools.types import ToolContext, ToolDefinition, ToolGates, ToolResult

# Description must be byte-identical to the current router_tools.py
# ToolSpec.description for web_fetch (= the literal from the E2 block).
# Copied verbatim.
_WEB_FETCH_DESCRIPTION = (
    "Fetch a single URL and return its (text-extracted) "
    "content. url: absolute http/https URL. "
    "max_length: cap on returned content size "
    "(default 50000). Use after web_search to read a "
    "result page in detail."
)

# Parameters JSON schema must be byte-identical to the current
# router_tools.py ToolSpec.parameters for web_fetch.
_WEB_FETCH_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "url": {"type": "string"},
        "max_length": {"type": "integer"},
    },
    "required": ["url"],
}


async def _handle(args: Mapping[str, Any], ctx: ToolContext) -> ToolResult:
    """Adapter wrapping op_runtime.web.handle_web_fetch.

    Bridges between the unified (args, ctx) signature and the
    existing (op, ctx, caller) signature. Once M3 Wave 1 succeeds,
    the body of handle_web_fetch may be inlined here in M4 cleanup.

    OpContext resolution (parallel with ``file.py:_build_legacy_op_context``):
      Preferred — ``ctx.router_state.op_context_factory()`` so the
        OpContext carries the session's PermissionResolver,
        PermissionDecl, and InterventionBus. This is what makes
        ``web.fetch: deny`` actually raise on the router-invoked
        path (#53 fix).
      Fallback — minimal synthesis from ToolContext fields. Used by
        phase-side dispatch and narrow test sites that don't exercise
        permission gating. ``intervention_bus=None`` is acceptable
        here because the fallback path doesn't have a session bus to
        reuse anyway.
    """
    # Lazy import to avoid circular dependency at registry-init time.
    from reyn.op_runtime.context import OpContext
    from reyn.op_runtime.web import handle_web_fetch
    from reyn.permissions.permissions import PermissionDecl
    from reyn.schemas.models import WebFetchIROp

    op = WebFetchIROp(
        kind="web_fetch",
        url=args["url"],
        max_length=int(args.get("max_length", 50_000)),
    )

    rs = ctx.router_state
    if rs is not None and rs.op_context_factory is not None:
        legacy_ctx = rs.op_context_factory()
    else:
        legacy_ctx = OpContext(
            workspace=ctx.workspace,
            events=ctx.events,
            permission_decl=PermissionDecl(),
            permission_resolver=ctx.permission_resolver,
            skill_name="",
            subscribers=getattr(ctx.events, "subscribers", []),
        )

    return await handle_web_fetch(op=op, ctx=legacy_ctx, caller="control_ir")


WEB_FETCH = ToolDefinition(
    name="web_fetch",
    description=_WEB_FETCH_DESCRIPTION,
    parameters=_WEB_FETCH_PARAMETERS,
    gates=ToolGates(router="allow", phase="allow"),
    handler=_handle,
    category="discovery",
    purity="read_only",   # web fetch reads a URL, no workspace side effect
)
