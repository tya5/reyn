"""emit_hook_event ToolDefinition (Hook-Event Redesign Phase 5 part 2,
proposal ``docs/deep-dives/proposals/0059-hook-event-redesign.md`` §8).

Router-only (``gates.phase="deny"``) — the handler needs a live, session-bound
``HookBus`` (``ctx.hook_bus``) + session identity (``ctx.session_id``), which
only the chat-router ``OpContext`` builders (``build_router_op_context`` /
``RouterHostAdapter.make_router_op_context``) wire; a static-execution phase
OpContext has neither, so the op would only ever fail-closed there — denying
at the tool-gate is more legible than a guaranteed-denied op call.

OpContext resolution mirrors ``mcp_drop.py``: prefer the router factory
(``ctx.router_state.op_context_factory()`` — the SAME OpContext the chat
session already built with ``hook_bus``/``session_id`` populated), falling
back to a minimal direct ``OpContext`` for router-adjacent test contexts
(which has no ``hook_bus``/``session_id`` → the op fails closed, matching
its documented no-live-session behavior).
"""
from __future__ import annotations

from typing import Any, Mapping

from reyn.tools.descriptions import hooks as _hooks_descriptions
from reyn.tools.types import ToolContext, ToolDefinition, ToolGates, ToolResult

_EMIT_HOOK_EVENT_DESCRIPTION = _hooks_descriptions.emit_hook_event.text

_EMIT_HOOK_EVENT_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "event_name": {
            "type": "string",
            "description": _hooks_descriptions.PARAMS["emit_hook_event"]["event_name"].text,
        },
        "payload": {
            "type": "object",
            "description": _hooks_descriptions.PARAMS["emit_hook_event"]["payload"].text,
        },
    },
    "required": ["event_name"],
}


async def _handle_emit_hook_event(
    args: Mapping[str, Any], ctx: ToolContext,
) -> ToolResult:
    """Adapter wrapping ``op_runtime.emit_hook_event.handle``.

    Builds an ``EmitHookEventIROp`` from ``args`` and dispatches through
    op_runtime, which owns the full autonomy-boundary enforcement (kind
    whitelist + structural session-binding — see that module's docstring).
    """
    from reyn.core.op_runtime.context import OpContext
    from reyn.core.op_runtime.emit_hook_event import handle as emit_handle
    from reyn.schemas.models import EmitHookEventIROp
    from reyn.security.permissions.permissions import PermissionDecl

    event_name = str(args["event_name"])
    payload = args.get("payload") or {}
    if not isinstance(payload, dict):
        return {"status": "error", "error": "payload must be an object"}

    op = EmitHookEventIROp(kind="emit_hook_event", event_name=event_name, payload=payload)

    if (
        ctx.router_state is not None
        and ctx.router_state.op_context_factory is not None
    ):
        legacy_ctx = ctx.router_state.op_context_factory()
    else:
        # No router factory (e.g. a router-adjacent test ToolContext) — a
        # minimal OpContext with no hook_bus/session_id wired. The op
        # handler fails closed (EmitHookEventDenied) rather than silently
        # no-op'ing; that is the documented behavior for a non-live-session
        # OpContext, not a special case here.
        legacy_ctx = OpContext(
            workspace=ctx.workspace,
            events=ctx.events,
            permission_decl=PermissionDecl(),
            permission_resolver=ctx.permission_resolver,
            actor="emit_hook_event",
        )

    return await emit_handle(op=op, ctx=legacy_ctx)


from reyn.core.offload.canonical import emit_hook_event_to_canonical  # noqa: E402

EMIT_HOOK_EVENT = ToolDefinition(
    canonical=emit_hook_event_to_canonical,
    name="emit_hook_event",
    description=_EMIT_HOOK_EVENT_DESCRIPTION,
    parameters=_EMIT_HOOK_EVENT_PARAMETERS,
    gates=ToolGates(router="allow", phase="deny"),
    handler=_handle_emit_hook_event,
    category="hooks",
    purity="side_effect",
)
