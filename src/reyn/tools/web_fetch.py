"""web_fetch ToolDefinition — Wave 1 migration (ADR-0026 M3).

Mirrors web_search.py structure. The existing handler in
src/reyn/op_runtime/web.py is preserved and wrapped via a thin
adapter that translates between the old (op, ctx) signature
and the new (args, ctx) signature.

The router dispatch path consumes this ToolDefinition; Wave 1 verifies
byte-identity against the pre-migration ToolSpec.
"""
from __future__ import annotations

from typing import Any, Mapping

from reyn.tools.descriptions import discovery
from reyn.tools.types import ToolContext, ToolDefinition, ToolGates, ToolResult

# The description text lives in src/reyn/tools/descriptions/discovery.py
# (Phase 1 of the tool-description package refactor); this alias keeps the
# call site unchanged. The wording stays purely descriptive — no behavioural
# guidance about WHEN to fetch (= sandbox_2 cofounder warning (b): keep the
# LLM's decision driven by the tool schema, not by prompt-engineered
# instructions).
_WEB_FETCH_DESCRIPTION = discovery.web_fetch.text

# #3580 ③: ``url`` is the ONLY LLM-settable argument. ``max_length`` used to sit
# here as a per-tool size cap; it is gone with nothing replacing it (owner ruling
# on #3580: 「わけわからんオレオレ仕様なんて廃止して」 — abolish the bespoke
# per-tool scheme rather than add a second mechanism). The size ceiling on what
# reaches the model's context is the OS-level tool-result cap alone
# (``offload.enabled``, default false), not anything web_fetch owns.
_WEB_FETCH_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "url": {"type": "string"},
    },
    "required": ["url"],
}


async def _handle(args: Mapping[str, Any], ctx: ToolContext) -> ToolResult:
    """Adapter wrapping op_runtime.web.handle_web_fetch.

    Bridges between the unified (args, ctx) signature and the
    existing (op, ctx) signature. Once M3 Wave 1 succeeds,
    the body of handle_web_fetch may be inlined here in M4 cleanup.

    OpContext resolution (parallel with ``file.py:_build_legacy_op_context``):
      Preferred — ``ctx.router_state.op_context_factory()`` so the
        OpContext carries the session's PermissionResolver,
        PermissionDecl, and InterventionBus. This is what makes
        ``web.fetch: deny`` actually raise on the router-invoked
        path (#53 fix).
      Fallback — minimal synthesis from ToolContext fields. Used by
        narrow test sites that don't exercise permission gating.
        ``ToolContext`` has no ``intervention_bus`` field at all, so
        this fallback's own ``legacy_ctx`` is unconditionally built
        with ``intervention_bus=None`` (the ``OpContext`` field's own
        default). #6146 co-vet finding (lead-coder, 2026-09-11): an
        EARLIER version of this docstring claimed that combination was
        safe because "the handler will raise the explicit RuntimeError
        above if a PermissionResolver is also present" — that claim
        was FALSE. The only such `RuntimeError` in
        ``op_runtime/web.py`` guards the multimodal/binary-media branch
        alone (fires only for an image response, ~111 lines below the
        real gate) — it never runs for ``require_http_get`` itself, so
        a resolver-present + bus-less fallback call reached that gate
        with no protection at all. Fixed below: this fallback now
        raises its OWN ``RuntimeError`` up front whenever
        ``ctx.permission_resolver`` is present, rather than relying on
        a guard elsewhere that never covered this path.
    """
    # Lazy import to avoid circular dependency at registry-init time.
    from reyn.core.op_runtime.context import OpContext
    from reyn.core.op_runtime.web import handle_web_fetch
    from reyn.schemas.models import WebFetchIROp
    from reyn.security.permissions.permissions import PermissionDecl

    op = WebFetchIROp(
        kind="web_fetch",
        url=args["url"],
    )

    rs = ctx.router_state
    if rs is not None and rs.op_context_factory is not None:
        legacy_ctx = rs.op_context_factory()
    else:
        # Narrow test sites + future surfaces without a router factory.
        # #6146 co-vet: ``ToolContext`` has no ``intervention_bus`` field,
        # so this branch's own ``legacy_ctx`` is unconditionally built
        # with ``intervention_bus=None`` below. A present
        # ``permission_resolver`` would then reach ``require_http_get``
        # (and every other gate) with no bus to prompt on — silently
        # falling back to the "no bus" behaviour (deny for anything not
        # already approved) instead of the interactive gate a real
        # PermissionResolver implies. Refuse the combination outright
        # rather than let it run degraded: a caller that has a real
        # resolver belongs on the ``op_context_factory`` path above, not
        # this narrow one.
        if ctx.permission_resolver is not None:
            raise RuntimeError(
                "web_fetch's fallback OpContext synthesis has no "
                "intervention_bus to offer (ToolContext carries none), but "
                "ctx.permission_resolver is set -- this combination would "
                "silently run every permission gate with no bus available. "
                "Route this call through ctx.router_state.op_context_factory() "
                "instead, which carries a real InterventionBus."
            )
        legacy_ctx = OpContext(
            workspace=ctx.workspace,
            events=ctx.events,
            permission_decl=PermissionDecl(),
            permission_resolver=ctx.permission_resolver,
            actor="",
            subscribers=getattr(ctx.events, "subscribers", []),
            # #1673: never resolver=None (the bug-class invariant). web_fetch makes
            # no LLM call, but the uniform threading keeps the invariant provable.
            resolver=ctx.resolver,
        )

    return await handle_web_fetch(op=op, ctx=legacy_ctx)


from reyn.core.offload.canonical import web_fetch_to_canonical  # noqa: E402

WEB_FETCH = ToolDefinition(
    canonical=web_fetch_to_canonical,
    name="web_fetch",
    router_dispatched=True,
    description=_WEB_FETCH_DESCRIPTION,
    parameters=_WEB_FETCH_PARAMETERS,
    gates=ToolGates(router="allow"),
    handler=_handle,
    category="discovery",
    purity="read_only",   # web fetch reads a URL, no workspace side effect
    returns_external_content=True,  # FP-0050/#1822: internet content
)
