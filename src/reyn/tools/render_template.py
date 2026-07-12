"""render_template ToolDefinition (#2692 / FP-0055) — chat + pipeline invocation surface.

The ``render_template`` op handler (``op_runtime/render_template.py``) and IR op model
(``RenderTemplateIROp``) already existed, but had NO ``ToolDefinition`` — so the default chat catalog
never offered it and ``registry.lookup("render_template")`` returned ``None``, making a pipeline
``tool: render_template`` step raise "not a registered tool". This module adds the single
``ToolDefinition`` that opens BOTH surfaces from the one unified registry (pipeline via bare-name
lookup, chat via ``gates.router="allow"`` + the ``build_tools`` section), mirroring ``tools/compact.py``.

``render_template`` is a sandboxed producer: ``data + Jinja2 template → string``. It has no sink — the
rendered string comes back as an ordinary op result (canonical ``text``) the caller routes wherever it
wants (a ``present``, a ``write_file`` step, a pipeline ``ctx.<name>``). The handler builds the real
``RenderTemplateIROp`` and dispatches through ``execute_op`` with the real ``OpContext``, so
``template_ref`` / ``data_ref`` reads go through the same ``file.read`` gate the op already enforces —
no permission bypass. Op handler + IR op model UNCHANGED. Tool name == op kind
(``"render_template"``) so FP-0056 identity dispatch reuses the existing op canonical mapper
(``render_template_to_canonical``) across every surface.

The XOR constraints (exactly one of ``template`` / ``template_ref``; exactly one of ``data_ref`` /
``data_inline``) are enforced by the ``RenderTemplateIROp`` validator — surfaced in the description,
not re-implemented.

Per ADR-0026: the ToolDefinition lives here; registration is in get_default_registry() in
tools/__init__.py.
"""
from __future__ import annotations

from typing import Any, Mapping

from reyn.core.offload.canonical import render_template_to_canonical
from reyn.tools.descriptions import presentation as _presentation_descriptions
from reyn.tools.types import ToolContext, ToolDefinition, ToolGates, ToolResult

# Relocated to reyn.tools.descriptions.presentation (Phase 3 tool-description
# package refactor — byte-identical, no LLM-facing text change).
_RENDER_TEMPLATE_DESCRIPTION = _presentation_descriptions.render_template.text

_RENDER_TEMPLATE_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "template": {
            "type": "string",
            "description": _presentation_descriptions.PARAMS["render_template"]["template"].text,
        },
        "template_ref": {
            "type": "string",
            "description": (
                _presentation_descriptions.PARAMS["render_template"]["template_ref"].text
            ),
        },
        "data_ref": {
            "type": "string",
            "description": _presentation_descriptions.PARAMS["render_template"]["data_ref"].text,
        },
        "data_inline": {
            "type": "object",
            "description": (
                _presentation_descriptions.PARAMS["render_template"]["data_inline"].text
            ),
        },
        "undefined": {
            "type": "string",
            "enum": ["strict", "lenient"],
            "description": _presentation_descriptions.PARAMS["render_template"]["undefined"].text,
        },
    },
    "required": [],
}


async def _handle_render_template(args: Mapping[str, Any], ctx: ToolContext) -> ToolResult:
    """Dispatch the render_template op via op_runtime.

    Builds a RenderTemplateIROp from the tool args and calls the registered handler with the real
    OpContext from ctx.router_state's factory (the compact.py precedent), or a minimal context
    otherwise. The real OpContext carries the caller's permission_decl so template_ref / data_ref
    reads go through the file.read gate — the tool never bypasses it. An arg-level XOR violation is
    caught as a clean error, not a crash.
    """
    from pydantic import ValidationError

    from reyn.core.op_runtime import execute_op
    from reyn.core.op_runtime.context import OpContext
    from reyn.schemas.models import RenderTemplateIROp
    from reyn.security.permissions.permissions import PermissionDecl

    undefined = args.get("undefined")
    try:
        op = RenderTemplateIROp(
            kind="render_template",
            template=args.get("template"),
            template_ref=args.get("template_ref"),
            data_ref=args.get("data_ref"),
            data_inline=args.get("data_inline"),
            undefined=undefined if undefined is not None else "strict",
        )
    except ValidationError as exc:
        return {"kind": "render_template", "status": "error", "ok": False, "error": str(exc)}

    if (
        ctx.router_state is not None
        and ctx.router_state.op_context_factory is not None
    ):
        legacy_ctx = ctx.router_state.op_context_factory()
    else:
        # Minimal context: an empty permission_decl (deny-by-default read-authority).
        # An inline-only render is pure computation; a ref-read is denied unless the
        # real OpContext (with the caller's grants) is threaded in. #1673: never
        # resolver=None (the bug-class invariant).
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


RENDER_TEMPLATE = ToolDefinition(
    canonical=render_template_to_canonical,
    name="render_template",
    router_dispatched=True,
    description=_RENDER_TEMPLATE_DESCRIPTION,
    parameters=_RENDER_TEMPLATE_PARAMETERS,
    gates=ToolGates(router="allow", phase="allow"),
    handler=_handle_render_template,
    category="presentation",
    purity="read_only",
)
