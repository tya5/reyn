"""file_* ToolDefinitions — fine-grained file ops migration (ADR-0026 M3 Wave 2).

Per ADR-0026 Open Q #6: adopt router-side fine-grained names as canonical
(= read_file, write_file, delete_file, list_directory). The phase-side
coarse-grained `file` op with `op` discriminator is the legacy form
that gets unbundled here.

Per ADR-0026 Open Q #7: existing phase frontmatter `allowed_ops: ["file"]`
will continue to work via prefix-wildcard semantics in the phase dispatcher
(= M4 cleanup). For now, the 4 ToolDefinitions are registered with both
router and phase gates allowed; phase-side dispatch unchanged in M3.

Important: FileIROp uses `op` (not `action`) as the discriminator field.
The `list_directory` router tool maps to `op="glob"` with a synthesised
glob pattern (`<path>/*`) — there is no `list_directory` op in FileIROp.
This is consistent with session.py `_file_list_directory` which also
delegates to the glob op internally.
"""
from __future__ import annotations

from typing import Any, Mapping

from reyn.tools.types import ToolDefinition, ToolGates, ToolContext, ToolResult


# Descriptions must be byte-identical to the ToolSpec.description literals in
# router_tools.py lines ~546-614 (C1-C4 block). Copied verbatim.

_LIST_DIRECTORY_DESCRIPTION = (
    "List contents of a directory under the agent's read scope. "
    "Returns names + types (file/dir)."
)

_READ_FILE_DESCRIPTION = (
    "Read a file's contents under the agent's read scope. "
    "Common conventions: README is at project root as "
    "`README.md`. CLAUDE.md, CHANGELOG.md, and "
    "configuration files (e.g. `reyn.yaml`, "
    "`pyproject.toml`) are at project root. Try these "
    "conventional paths directly instead of asking the "
    "user where the file lives."
)

_WRITE_FILE_DESCRIPTION = (
    "Write content to a file under the agent's write scope. "
    "Creates or overwrites."
)

_DELETE_FILE_DESCRIPTION = (
    "Delete a file under the agent's write scope."
)

# Parameters JSON schemas must be byte-identical to the ToolSpec.parameters
# literals in router_tools.py lines ~546-614 (C1-C4 block). Copied verbatim.

_LIST_DIRECTORY_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "path": {"type": "string"},
    },
    "required": ["path"],
}

_READ_FILE_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "path": {"type": "string"},
    },
    "required": ["path"],
}

_WRITE_FILE_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "path": {"type": "string"},
        "content": {"type": "string"},
    },
    "required": ["path", "content"],
}

_DELETE_FILE_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "path": {"type": "string"},
    },
    "required": ["path"],
}


def _build_legacy_op_context(ctx: ToolContext) -> Any:
    """Build an OpContext for op_runtime delegation.

    Preferred (= router-side production, ADR-0026 Phase 3.5): use the
    ``ctx.router_state.op_context_factory`` callable bound by
    RouterLoop. The factory yields the same OpContext the legacy router
    branches received — populated PermissionDecl (= operator file/mcp
    declarations), Workspace with ``skill_name="chat_router"``, and the
    flattened MCP servers map.

    Fallback (= phase-side dispatch, test sites): synthesize a minimal
    OpContext from ToolContext fields with ``PermissionDecl()`` empty.
    The fallback is documented as M3 transitional in ADR-0026 Open Q #7;
    callers that need real permission gating must populate
    ``router_state.op_context_factory`` (router) or supply
    ``phase_state.op_context`` (phase) when those wirings land.
    """
    rs = ctx.router_state
    if rs is not None and rs.op_context_factory is not None:
        return rs.op_context_factory()

    from reyn.op_runtime.context import OpContext
    from reyn.permissions.permissions import PermissionDecl

    return OpContext(
        workspace=ctx.workspace,
        events=ctx.events,
        permission_decl=PermissionDecl(),
        permission_resolver=ctx.permission_resolver,
        skill_name="",
    )


async def _handle_read(args: Mapping[str, Any], ctx: ToolContext) -> ToolResult:
    """Adapter for read_file — delegates to op_runtime file handler.

    Builds FileIROp(op="read") and routes via execute_op.
    """
    from reyn.op_runtime import execute_op
    from reyn.schemas.models import FileIROp

    op = FileIROp(kind="file", op="read", path=args["path"])
    legacy_ctx = _build_legacy_op_context(ctx)
    return await execute_op(op, legacy_ctx, caller="control_ir")


async def _handle_write(args: Mapping[str, Any], ctx: ToolContext) -> ToolResult:
    """Adapter for write_file — delegates to op_runtime file handler.

    Builds FileIROp(op="write") with path and content from args.
    """
    from reyn.op_runtime import execute_op
    from reyn.schemas.models import FileIROp

    op = FileIROp(kind="file", op="write", path=args["path"], content=args["content"])
    legacy_ctx = _build_legacy_op_context(ctx)
    return await execute_op(op, legacy_ctx, caller="control_ir")


async def _handle_delete(args: Mapping[str, Any], ctx: ToolContext) -> ToolResult:
    """Adapter for delete_file — delegates to op_runtime file handler.

    Builds FileIROp(op="delete").
    """
    from reyn.op_runtime import execute_op
    from reyn.schemas.models import FileIROp

    op = FileIROp(kind="file", op="delete", path=args["path"])
    legacy_ctx = _build_legacy_op_context(ctx)
    return await execute_op(op, legacy_ctx, caller="control_ir")


async def _handle_list(args: Mapping[str, Any], ctx: ToolContext) -> ToolResult:
    """Adapter for list_directory — delegates to op_runtime file handler.

    FileIROp has no `list_directory` op variant; directory listing is
    implemented via op="glob" with a synthesised `<path>/*` pattern.
    This matches the router session._file_list_directory implementation
    exactly (= single canonical approach, no divergence).

    Path normalisation: map "" / "/" / "./" to "." so the LLM's typical
    "list files here" intent resolves to cwd rather than filesystem root.
    """
    from reyn.op_runtime import execute_op
    from reyn.schemas.models import FileIROp

    path = args["path"]
    if path in ("", "/", "./"):
        path = "."

    op = FileIROp(kind="file", op="glob", path=f"{path.rstrip('/')}/*")
    legacy_ctx = _build_legacy_op_context(ctx)
    result = await execute_op(op, legacy_ctx, caller="control_ir")

    # Normalise to {path, entries} shape (= same as session._file_list_directory)
    if result.get("status") == "ok":
        return {"path": path, "entries": result.get("matches", [])}
    return {"error": result.get("error", "list_directory failed")}


READ_FILE = ToolDefinition(
    name="read_file",
    description=_READ_FILE_DESCRIPTION,
    parameters=_READ_FILE_PARAMETERS,
    gates=ToolGates(router="allow", phase="allow"),
    handler=_handle_read,
    category="io",
    purity="read_only",
)

WRITE_FILE = ToolDefinition(
    name="write_file",
    description=_WRITE_FILE_DESCRIPTION,
    parameters=_WRITE_FILE_PARAMETERS,
    gates=ToolGates(router="allow", phase="allow"),
    handler=_handle_write,
    category="io",
    purity="side_effect",
)

DELETE_FILE = ToolDefinition(
    name="delete_file",
    description=_DELETE_FILE_DESCRIPTION,
    parameters=_DELETE_FILE_PARAMETERS,
    gates=ToolGates(router="allow", phase="allow"),
    handler=_handle_delete,
    category="io",
    purity="side_effect",
)

LIST_DIRECTORY = ToolDefinition(
    name="list_directory",
    description=_LIST_DIRECTORY_DESCRIPTION,
    parameters=_LIST_DIRECTORY_PARAMETERS,
    gates=ToolGates(router="allow", phase="allow"),
    handler=_handle_list,
    category="io",
    purity="read_only",
)


# ── Coarse-name "file" ToolDefinition (phase-side, ADR-0026 Phase 4) ──────────
#
# The phase Control IR uses ``kind: "file"`` with an ``op`` discriminator
# (= read / write / delete / glob / grep / edit / regenerate_index — 7 sub-ops).
# This single ToolDefinition exposes the full FileIROp schema phase-side and
# delegates to the canonical op_runtime/file.py handler.  Router-side stays
# fine-grained (= read_file / write_file / delete_file / list_directory) so
# the LLM-visible router catalog is unchanged.
#
# After Phase 4 step 2 wires ControlIRExecutor through the registry,
# OP_KIND_MODEL_MAP becomes a derived view and the schema source for
# phase-side dispatch is this ToolDefinition's parameters (= FileIROp).

def _build_file_parameters() -> dict[str, Any]:
    """Derive the parameters JSON schema from FileIROp Pydantic model."""
    from reyn.schemas.models import FileIROp
    schema = FileIROp.model_json_schema()
    required = [f for f in schema.get("required", []) if f != "kind"]
    properties = {k: v for k, v in schema.get("properties", {}).items() if k != "kind"}
    return {"type": "object", "properties": properties, **({"required": required} if required else {})}


_FILE_DESCRIPTION = (
    "File operations (= phase-side coarse op).  ``op`` discriminator "
    "selects the sub-operation: read / write / delete / glob / grep / "
    "edit / regenerate_index.  See FileIROp schema for per-op fields.  "
    "All paths are relative to the workspace root and gated by the "
    "phase's permissions.file_read / file_write declarations."
)


async def _handle_file(args: Mapping[str, Any], ctx: ToolContext) -> ToolResult:
    """Coarse handler for the phase-side ``file`` op.  Delegates to op_runtime."""
    from reyn.op_runtime import execute_op
    from reyn.schemas.models import FileIROp

    op = FileIROp(kind="file", **{k: v for k, v in args.items() if k != "kind"})
    legacy_ctx = _build_legacy_op_context(ctx)
    return await execute_op(op, legacy_ctx, caller="control_ir")


FILE_OP = ToolDefinition(
    name="file",
    description=_FILE_DESCRIPTION,
    parameters=_build_file_parameters(),
    gates=ToolGates(router="deny", phase="allow"),
    handler=_handle_file,
    category="io",
    purity="side_effect",
)
