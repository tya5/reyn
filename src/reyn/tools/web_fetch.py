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

# Description updated by #385 PoC PR-D: when MediaStore is available
# (= default production path), the tool returns a structured ``preview``
# block + ``path_ref`` under ``.reyn/tool-results/`` instead of inlining
# the full extracted body. The ``read_tool_result(path=...)`` tool loads
# the full body on demand. The wording stays purely descriptive — no
# behavioural guidance about WHEN to expand (= sandbox_2 cofounder
# warning (b): keep the LLM's decision driven by the tool schema, not
# by prompt-engineered instructions).
_WEB_FETCH_DESCRIPTION = (
    "Fetch a single URL. Returns a structured preview "
    "(title, outline, first paragraph, link count for HTML; "
    "first lines for text) plus a path_ref to the full body "
    "stored under .reyn/tool-results/. url: absolute http/https URL. "
    "max_length: cap on extracted body length (default 50000). "
    "Use after web_search to load a result page; call "
    "file__read(path) to read the full body."
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
    from reyn.core.op_runtime.context import OpContext
    from reyn.core.op_runtime.web import handle_web_fetch
    from reyn.schemas.models import WebFetchIROp
    from reyn.security.permissions.permissions import PermissionDecl

    op = WebFetchIROp(
        kind="web_fetch",
        url=args["url"],
        max_length=int(args.get("max_length", 50_000)),
    )

    rs = ctx.router_state
    ps = ctx.phase_state
    if rs is not None and rs.op_context_factory is not None:
        legacy_ctx = rs.op_context_factory()
    elif ps is not None and getattr(ps, "op_context", None) is not None:
        # Phase-side dispatch: reuse the real OpContext built by
        # control_ir_executor — it carries the InterventionBus that
        # ``handle_web_fetch`` needs for the Tier 1 4-layer approval
        # check (= ``op_runtime/web.py``). Without this, a phase that
        # legitimately emits a ``web_fetch`` op (e.g. ``skill_importer``
        # search/convert) fails with ``RuntimeError: web_fetch op
        # requires intervention_bus on OpContext``.
        legacy_ctx = ps.op_context
    else:
        # Narrow test sites + future surfaces that aren't router/phase.
        # ``intervention_bus=None`` is acceptable only because the
        # fallback path doesn't have any bus to reuse anyway; the
        # handler will raise the explicit RuntimeError above if a
        # PermissionResolver is also present.
        legacy_ctx = OpContext(
            workspace=ctx.workspace,
            events=ctx.events,
            permission_decl=PermissionDecl(),
            permission_resolver=ctx.permission_resolver,
            skill_name="",
            subscribers=getattr(ctx.events, "subscribers", []),
            # #1673: never resolver=None (the bug-class invariant). web_fetch makes
            # no LLM call, but the uniform threading keeps the invariant provable.
            resolver=ctx.resolver,
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
    returns_external_content=True,  # FP-0050/#1822: internet content
)
