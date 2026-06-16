"""drop_source ToolDefinition (ADR-0033 Phase 1).

DROP_SOURCE is both router-callable and phase-callable (gates.router=allow,
gates.phase=allow). It is the LLM entry point for removing an indexed source
entirely (SQLite backend + manifest entry).

The handler delegates to op_runtime.index_drop.handle, which:
  1. Gates via permission resolver (index_drop: ask default, ADR-0029 mirror)
  2. Calls SqliteIndexBackend.drop
  3. Removes the SourceManifest entry
  4. Emits ``index_dropped`` P6 event

Per ADR-0026: the ToolDefinition lives here; registration is in
get_default_registry() in tools/__init__.py.
"""
from __future__ import annotations

from typing import Any, Mapping

from reyn.tools.types import ToolContext, ToolDefinition, ToolGates, ToolResult

_DROP_SOURCE_DESCRIPTION = (
    "Remove an indexed source entirely (= delete its SQLite + manifest entry). "
    "Use when retiring trial sources or replacing with a different strategy. "
    "Permission-gated; user is prompted to confirm."
)

_DROP_SOURCE_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "source": {
            "type": "string",
            "description": (
                "Logical source name to remove (from Indexed sources list)."
            ),
        },
    },
    "required": ["source"],
}


async def _handle_drop_source(
    args: Mapping[str, Any], ctx: ToolContext
) -> ToolResult:
    """Dispatch the index_drop op via op_runtime.

    Builds an IndexDropIROp from args and delegates to the registered
    index_drop handler, which owns the full lifecycle: permission gate,
    backend drop, manifest removal, and P6 event emit.
    """
    from reyn.op_runtime import execute_op
    from reyn.op_runtime.context import OpContext
    from reyn.schemas.models import IndexDropIROp
    from reyn.security.permissions.permissions import PermissionDecl

    # B34 LLM-attractor fix: accept common synonyms before KeyError.
    # LLM sends {source_id:...} instead of {source:...} — observed B33 W4 S6.
    # Canonical key wins when both are present.
    if "source" not in args and "source_id" in args:
        args = {**args, "source": args["source_id"]}

    op = IndexDropIROp(
        kind="index_drop",
        source=str(args["source"]),
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
        # #571 collapse arc Phase 5: explicit file.write axis replaces
        # the former index_drop bool axis. Tool wrapper synthesises the
        # decl + session-approves so the op handler's require_file_write
        # passes silently (= tool-level authorisation already happened
        # at the calling skill's permissions.tool gate).
        canonical_manifest = ".reyn/index/sources.yaml"
        if ctx.permission_resolver is not None:
            ctx.permission_resolver.session_approve_path(
                canonical_manifest, "drop_source", "file.write",
            )
        legacy_ctx = OpContext(
            workspace=ctx.workspace,
            events=ctx.events,
            permission_decl=PermissionDecl(
                file_write=[{"path": canonical_manifest, "scope": "just_path"}],
            ),
            permission_resolver=ctx.permission_resolver,
            skill_name="drop_source",
            intervention_bus=None,
            subscribers=getattr(ctx.events, "subscribers", []),
        )

    return await execute_op(op, legacy_ctx, caller="control_ir")


DROP_SOURCE = ToolDefinition(
    name="drop_source",
    description=_DROP_SOURCE_DESCRIPTION,
    parameters=_DROP_SOURCE_PARAMETERS,
    gates=ToolGates(router="allow", phase="allow"),
    handler=_handle_drop_source,
    category="io",
    purity="side_effect",
)
