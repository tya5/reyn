"""present ToolDefinition (#2692 / FP-0054 / FP-0055) — chat + pipeline invocation surface.

The ``present`` op handler (``op_runtime/present.py``) and IR op model (``PresentIROp``) already
existed and worked, but had NO invocation surface: no ``ToolDefinition`` meant the default chat JSON
tool catalog never offered it AND ``registry.lookup("present")`` returned ``None`` so a pipeline
``tool: present`` step raised a "not a registered tool" error. The headline present-layer arc was
reachable from nowhere (#2688 sweep).

This module is the additive fix: a single ``ToolDefinition`` registered in ``get_default_registry()``
opens BOTH surfaces from one lever (they draw the same unified registry) — pipeline via bare-name
lookup, chat via ``gates.router="allow"`` + the ``build_tools`` section. The handler builds the real
``PresentIROp`` and dispatches through ``execute_op`` with the real ``OpContext`` (the ``compact``
precedent, ``tools/compact.py``), so the existing op handler enforces ``present``'s ``data_ref``
read-authority (identical to ``file.read``) — the tool adds no permission bypass. Op handler + IR op
model are UNCHANGED. Tool name == op kind (``"present"``) so FP-0056 identity dispatch resolves the
same canonical declaration across every surface.

Tiered schema (minimise LLM burden — "display is free"): the minimal call is ``present(data_ref=...)``,
which routes straight to the stage-3 default-viewer synthesis (no view authoring). ``view`` names a
registered presentation; ``blueprint`` is the advanced declarative component tree. The XOR constraints
(exactly one of ``data_ref`` / ``data_inline``; at most one of ``view`` / ``blueprint``) are enforced
by the ``PresentIROp`` validator — surfaced here in the description, not re-implemented.

Per ADR-0026: the ToolDefinition lives here; registration is in get_default_registry() in
tools/__init__.py.
"""
from __future__ import annotations

from typing import Any, Mapping

from reyn.core.offload.canonical import present_to_canonical
from reyn.tools.descriptions import presentation as _presentation_descriptions
from reyn.tools.types import ToolContext, ToolDefinition, ToolGates, ToolResult

# Relocated to reyn.tools.descriptions.presentation (Phase 3 tool-description
# package refactor — byte-identical, no LLM-facing text change).
_PRESENT_DESCRIPTION = _presentation_descriptions.present.text

# proposal 0060 D5d: the single doc_ref for the present op — used both as the
# ToolDefinition's structured pointer field and (D5c) the error-rail pointer
# appended to a schema-validation failure below.
_PRESENT_DOC_REF = "docs/concepts/runtime/present.md"

_PRESENT_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "data_ref": {
            "type": "string",
            "description": _presentation_descriptions.PARAMS["present"]["data_ref"].text,
        },
        "data_inline": {
            "type": "object",
            "description": _presentation_descriptions.PARAMS["present"]["data_inline"].text,
        },
        "view": {
            "type": "string",
            "description": _presentation_descriptions.PARAMS["present"]["view"].text,
        },
        "blueprint": {
            "type": "object",
            "description": _presentation_descriptions.PARAMS["present"]["blueprint"].text,
        },
    },
    "required": [],
}


async def _handle_present(args: Mapping[str, Any], ctx: ToolContext) -> ToolResult:
    """Dispatch the present op via op_runtime.

    Builds a PresentIROp from the tool args and calls the registered present handler with the real
    OpContext from ctx.router_state's factory (the compact.py precedent), or a minimal context
    otherwise. The real OpContext carries the caller's permission_decl so the op handler enforces
    data_ref read-authority == file.read — the tool never bypasses it. An arg-level XOR violation
    (both/neither source, both view+blueprint) is caught as a clean error, not a crash.
    """
    from pydantic import ValidationError

    from reyn.core.op_runtime import execute_op
    from reyn.core.op_runtime.context import OpContext
    from reyn.schemas.models import PresentIROp
    from reyn.security.permissions.permissions import PermissionDecl

    try:
        op = PresentIROp(
            kind="present",
            data_ref=args.get("data_ref"),
            data_inline=args.get("data_inline"),
            view=args.get("view"),
            blueprint=args.get("blueprint"),
        )
    except ValidationError as exc:
        from reyn.core.doc_ref_rail import with_doc_pointer

        return {
            "kind": "present",
            "status": "error",
            "ok": False,
            "error": with_doc_pointer(str(exc), _PRESENT_DOC_REF),
        }

    if (
        ctx.router_state is not None
        and ctx.router_state.op_context_factory is not None
    ):
        legacy_ctx = ctx.router_state.op_context_factory()
    else:
        # Minimal context: no presentation surface wired and an empty permission_decl
        # (deny-by-default read-authority). The op still runs — data_ref reads resolve
        # through the same file.read gate, so an unauthorized ref is denied, never
        # bypassed. #1673: never resolver=None (the bug-class invariant).
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


PRESENT = ToolDefinition(
    canonical=present_to_canonical,
    name="present",
    router_dispatched=True,
    description=_PRESENT_DESCRIPTION,
    parameters=_PRESENT_PARAMETERS,
    gates=ToolGates(router="allow", phase="allow"),
    handler=_handle_present,
    category="presentation",
    purity="side_effect",
    doc_ref=_PRESENT_DOC_REF,
)
