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
    """
    # Lazy import to avoid circular dependency at registry-init time.
    from reyn.op_runtime.context import OpContext
    from reyn.op_runtime.web import handle_web_fetch
    from reyn.permissions.permissions import PermissionDecl
    from reyn.schemas.models import WebFetchIROp

    # Build a transient WebFetchIROp from args (= reuse Pydantic
    # validation that the existing op handler expects).
    op = WebFetchIROp(
        kind="web_fetch",
        url=args["url"],
        max_length=int(args.get("max_length", 50_000)),
    )

    # Build a legacy OpContext from the new ToolContext.
    # OpContext.permission_decl is a required field with no equivalent
    # on ToolContext. We use PermissionDecl() (empty defaults = no
    # granted permissions) which is safe for web_fetch because the
    # handler does not perform permission checks (web fetch is
    # read-only / public URL reads). This is the only mandatory field
    # that ToolContext cannot supply; see adapter shim note in the
    # ADR-0026 M2 findings doc.
    #
    # events.subscribers: the existing OpContext constructor requires
    # this to forward subscribers to sub-skill invocations. Web fetch
    # does not spawn sub-skills, but OpContext.subscribers is set
    # defensively via getattr fallback.
    legacy_ctx = OpContext(
        workspace=ctx.workspace,
        events=ctx.events,
        permission_decl=PermissionDecl(),
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
