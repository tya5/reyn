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

from reyn.tools.types import ToolContext, ToolDefinition, ToolGates, ToolResult

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
    "Creates or overwrites the WHOLE file. For a partial or surgical "
    "change to an existing file, prefer the edit action instead of "
    "rewriting: describe_action(action_name='file__edit') for its args, "
    "then invoke_action."
)

_DELETE_FILE_DESCRIPTION = (
    "Delete a file under the agent's write scope."
)

_EDIT_FILE_DESCRIPTION = (
    "Replace a unique string in a file under the agent's write scope. "
    "`old_string` MUST appear exactly once in the file; if it appears "
    "multiple times, the call fails with a count — re-call with a longer "
    "context-including snippet, or pass `replace_all=true` to replace "
    "every occurrence. Use this for partial edits instead of read+write "
    "for the whole file."
)

_GREP_FILES_DESCRIPTION = (
    "Search for a regex pattern across files under the agent's read scope. "
    "Use this when you need to find text or code patterns in files — "
    "do NOT use list_directory for grep/glob intent. "
    "Returns matching lines with file paths and line numbers."
)

_GLOB_FILES_DESCRIPTION = (
    "Find files matching a glob pattern (e.g. '**/*.py') under the agent's "
    "read scope. Use `**` to recurse into subdirectories. Use this when you "
    "need to enumerate files by name pattern — do NOT use list_directory "
    "for glob intent. Returns a list of matching file paths."
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
        "offset": {
            "type": "integer",
            "description": (
                "Line number to start reading from (0-indexed). "
                "Omit to start at the beginning of the file."
            ),
        },
        "limit": {
            "type": "integer",
            "description": (
                "Number of lines to read from `offset`. "
                "Omit to read through end of file."
            ),
        },
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

_EDIT_FILE_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "path": {"type": "string"},
        "old_string": {
            "type": "string",
            "description": (
                "Exact text to replace. Must appear exactly once unless "
                "replace_all is true; include surrounding context to "
                "make it unique."
            ),
        },
        "new_string": {
            "type": "string",
            "description": "Replacement text.",
        },
        "replace_all": {
            "type": "boolean",
            "description": (
                "When true, every occurrence of old_string is replaced. "
                "Default false (= require uniqueness)."
            ),
        },
    },
    "required": ["path", "old_string", "new_string"],
}

_GREP_FILES_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "pattern": {
            "type": "string",
            "description": "Regex pattern to search for.",
        },
        "path": {
            "type": "string",
            "description": "Directory or file to search. Defaults to '.' (workspace root).",
        },
        "glob": {
            "type": "string",
            "description": "File-glob filter (e.g. '**/*.py'). Searches all files when omitted.",
        },
        "case_sensitive": {
            "type": "boolean",
            "description": "When true, search is case-sensitive. Defaults to false.",
        },
        "max_results": {
            "type": "integer",
            "description": "Maximum number of matches to return. Defaults to 50.",
        },
    },
    "required": ["pattern"],
}

_GLOB_FILES_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "pattern": {
            "type": "string",
            "description": (
                "Glob pattern. To match by name anywhere under a directory, "
                "always include `**` (e.g. '**/*.py' or 'src/**/*.md'). "
                "A bare name without `**` matches only at the exact root "
                "level, not recursively."
            ),
        },
        "path": {
            "type": "string",
            "description": "Root directory for the glob. Defaults to '.' (workspace root).",
        },
    },
    "required": ["pattern"],
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

    # Propagate the active phase's PermissionDecl via phase_state.op_context
    # (FP-0008 Tool→OpContext bridge fix 2026-05-28).
    phase_op_ctx = (
        ctx.phase_state.op_context if ctx.phase_state is not None else None
    )
    return OpContext(
        workspace=ctx.workspace,
        events=ctx.events,
        permission_decl=(
            phase_op_ctx.permission_decl
            if phase_op_ctx is not None
            else PermissionDecl()
        ),
        permission_resolver=ctx.permission_resolver,
        skill_name="",
    )


async def _handle_read(args: Mapping[str, Any], ctx: ToolContext) -> ToolResult:
    """Adapter for read_file — delegates to op_runtime file handler.

    Builds FileIROp(op="read") and routes via execute_op. The optional
    ``offset`` / ``limit`` line-slice args are forwarded to FileIROp (=
    already supported by ``op_runtime/file.py``).
    """
    from reyn.op_runtime import execute_op
    from reyn.schemas.models import FileIROp

    op = FileIROp(
        kind="file",
        op="read",
        path=args["path"],
        offset=args.get("offset"),
        limit=args.get("limit"),
    )
    legacy_ctx = _build_legacy_op_context(ctx)
    return await execute_op(op, legacy_ctx, caller="control_ir")


async def _handle_write(args: Mapping[str, Any], ctx: ToolContext) -> ToolResult:
    """Adapter for write_file — delegates to op_runtime file handler.

    Builds FileIROp(op="write") with path and content from args.
    """
    from reyn.op_runtime import execute_op
    from reyn.schemas.models import FileIROp

    # B34 LLM-attractor fix: accept common synonyms before KeyError.
    # LLM sends {text:...} instead of {content:...} — observed B33 W4 S1,
    # B30 W4 S1. Canonical key wins when both are present.
    if "content" not in args and "text" in args:
        args = {**args, "content": args["text"]}

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


async def _handle_edit(args: Mapping[str, Any], ctx: ToolContext) -> ToolResult:
    """Adapter for edit_file — delegates to op_runtime file handler.

    Builds FileIROp(op="edit"). The op_runtime handler enforces:
      * old_string must appear in the file (= 0 matches → error)
      * old_string must be unique unless replace_all=true (= multi-match
        without replace_all → error with count)
      * write permission gating (same tier as write_file / delete_file)
    """
    from reyn.op_runtime import execute_op
    from reyn.schemas.models import FileIROp

    op = FileIROp(
        kind="file",
        op="edit",
        path=args["path"],
        old_string=args["old_string"],
        new_string=args["new_string"],
        replace_all=bool(args.get("replace_all", False)),
    )
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


async def _handle_grep(args: Mapping[str, Any], ctx: ToolContext) -> ToolResult:
    """Adapter for grep_files — delegates to op_runtime file handler.

    Maps the router-side `case_sensitive` boolean to the op_runtime
    `case_insensitive` convention (= FileIROp.case_insensitive).
    """
    from reyn.op_runtime import execute_op
    from reyn.schemas.models import FileIROp

    case_sensitive = args.get("case_sensitive", False)
    op = FileIROp(
        kind="file",
        op="grep",
        path=args.get("path", "."),
        pattern=args["pattern"],
        glob=args.get("glob"),
        case_insensitive=not case_sensitive,
        head_limit=args.get("max_results", 50),
    )
    legacy_ctx = _build_legacy_op_context(ctx)
    return await execute_op(op, legacy_ctx, caller="control_ir")


async def _handle_glob(args: Mapping[str, Any], ctx: ToolContext) -> ToolResult:
    """Adapter for glob_files — delegates to op_runtime file handler.

    Combines `path` (root dir) and `pattern` (glob) into the FileIROp.path
    field that the glob op uses as its glob pattern. The op_runtime glob op
    interprets FileIROp.path as the full glob pattern, so we build
    `<path>/<pattern>` (or just `<pattern>` when path is absent / ".").
    """
    from reyn.op_runtime import execute_op
    from reyn.schemas.models import FileIROp

    root = args.get("path", ".").rstrip("/")
    pattern = args["pattern"]
    # Combine: if root is "." use pattern directly (avoids "./**/*.py" oddity
    # when workspace.glob_files is cwd-relative). Otherwise prefix the root.
    combined = pattern if root in ("", ".") else f"{root}/{pattern}"
    op = FileIROp(kind="file", op="glob", path=combined)
    legacy_ctx = _build_legacy_op_context(ctx)
    result = await execute_op(op, legacy_ctx, caller="control_ir")

    # Normalise: surface as {pattern, matches, count} for caller ergonomics.
    if result.get("status") == "ok":
        return {
            "pattern": combined,
            "matches": result.get("matches", []),
            "count": result.get("count", 0),
        }
    return {"error": result.get("error", "glob_files failed")}


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

EDIT_FILE = ToolDefinition(
    name="edit_file",
    description=_EDIT_FILE_DESCRIPTION,
    parameters=_EDIT_FILE_PARAMETERS,
    gates=ToolGates(router="allow", phase="allow"),
    handler=_handle_edit,
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

GREP_FILES = ToolDefinition(
    name="grep_files",
    description=_GREP_FILES_DESCRIPTION,
    parameters=_GREP_FILES_PARAMETERS,
    gates=ToolGates(router="allow", phase="allow"),
    handler=_handle_grep,
    category="io",
    purity="read_only",
)

GLOB_FILES = ToolDefinition(
    name="glob_files",
    description=_GLOB_FILES_DESCRIPTION,
    parameters=_GLOB_FILES_PARAMETERS,
    gates=ToolGates(router="allow", phase="allow"),
    handler=_handle_glob,
    category="io",
    purity="read_only",
)

