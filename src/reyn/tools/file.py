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

from reyn.tools.descriptions import io as _io_descriptions
from reyn.tools.op_context_bridge import (
    build_legacy_op_context as _build_legacy_op_context,
)
from reyn.tools.types import ToolContext, ToolDefinition, ToolGates, ToolResult

# Descriptions must be byte-identical to the ToolSpec.description literals in
# router_tools.py lines ~546-614 (C1-C4 block). Copied verbatim.
#
# Reviewable in src/reyn/tools/descriptions/io.py (Phase 2 of the
# tool-description package refactor) — these aliases keep the call sites
# unchanged (byte-identical relocation, no LLM-facing text change).

_LIST_DIRECTORY_DESCRIPTION = _io_descriptions.list_directory.text

_READ_FILE_DESCRIPTION = _io_descriptions.read_file.text

_WRITE_FILE_DESCRIPTION = _io_descriptions.write_file.text

_DELETE_FILE_DESCRIPTION = _io_descriptions.delete_file.text

_EDIT_FILE_DESCRIPTION = _io_descriptions.edit_file.text

_GREP_FILES_DESCRIPTION = _io_descriptions.grep_files.text

_GLOB_FILES_DESCRIPTION = _io_descriptions.glob_files.text

# Parameters JSON schemas must be byte-identical to the ToolSpec.parameters
# literals in router_tools.py lines ~546-614 (C1-C4 block). Copied verbatim.

_LIST_DIRECTORY_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "path": {"type": "string"},
        "max_results": {
            "type": "integer",
            "description": _io_descriptions.PARAMS["list_directory"]["max_results"].text,
        },
    },
    "required": ["path"],
}

_READ_FILE_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "path": {"type": "string"},
        "offset": {
            "type": "integer",
            "description": _io_descriptions.PARAMS["read_file"]["offset"].text,
        },
        "limit": {
            "type": "integer",
            "description": _io_descriptions.PARAMS["read_file"]["limit"].text,
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
            "description": _io_descriptions.PARAMS["edit_file"]["old_string"].text,
        },
        "new_string": {
            "type": "string",
            "description": _io_descriptions.PARAMS["edit_file"]["new_string"].text,
        },
        "replace_all": {
            "type": "boolean",
            "description": _io_descriptions.PARAMS["edit_file"]["replace_all"].text,
        },
    },
    "required": ["path", "old_string", "new_string"],
}

_GREP_FILES_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "pattern": {
            "type": "string",
            "description": _io_descriptions.PARAMS["grep_files"]["pattern"].text,
        },
        "path": {
            "type": "string",
            "description": _io_descriptions.PARAMS["grep_files"]["path"].text,
        },
        "glob": {
            "type": "string",
            "description": _io_descriptions.PARAMS["grep_files"]["glob"].text,
        },
        "case_sensitive": {
            "type": "boolean",
            "description": _io_descriptions.PARAMS["grep_files"]["case_sensitive"].text,
        },
        "max_results": {
            "type": "integer",
            "description": _io_descriptions.PARAMS["grep_files"]["max_results"].text,
        },
    },
    "required": ["pattern"],
}

_GLOB_FILES_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "pattern": {
            "type": "string",
            "description": _io_descriptions.PARAMS["glob_files"]["pattern"].text,
        },
        "path": {
            "type": "string",
            "description": _io_descriptions.PARAMS["glob_files"]["path"].text,
        },
        "max_results": {
            "type": "integer",
            "description": _io_descriptions.PARAMS["glob_files"]["max_results"].text,
        },
        "absolute": {
            "type": "boolean",
            "description": _io_descriptions.PARAMS["glob_files"]["absolute"].text,
        },
    },
    "required": ["pattern"],
}


async def _handle_read(args: Mapping[str, Any], ctx: ToolContext) -> ToolResult:
    """Adapter for read_file — delegates to op_runtime file handler.

    Builds FileIROp(op="read") and routes via execute_op. The optional
    ``offset`` / ``limit`` line-slice args are forwarded to FileIROp (=
    already supported by ``op_runtime/file.py``).
    """
    from reyn.core.op_runtime import execute_op
    from reyn.schemas.models import FileIROp

    op = FileIROp(
        kind="file",
        op="read",
        path=args["path"],
        offset=args.get("offset"),
        limit=args.get("limit"),
    )
    legacy_ctx = _build_legacy_op_context(ctx)
    return await execute_op(op, legacy_ctx)


async def _handle_write(args: Mapping[str, Any], ctx: ToolContext) -> ToolResult:
    """Adapter for write_file — delegates to op_runtime file handler.

    Builds FileIROp(op="write") with path and content from args.
    """
    from reyn.core.op_runtime import execute_op
    from reyn.schemas.models import FileIROp

    # B34 LLM-attractor fix: accept common synonyms before KeyError.
    # LLM sends {text:...} instead of {content:...} — observed B33 W4 S1,
    # B30 W4 S1. Canonical key wins when both are present.
    if "content" not in args and "text" in args:
        args = {**args, "content": args["text"]}

    op = FileIROp(kind="file", op="write", path=args["path"], content=args["content"])
    legacy_ctx = _build_legacy_op_context(ctx)
    return await execute_op(op, legacy_ctx)


async def _handle_delete(args: Mapping[str, Any], ctx: ToolContext) -> ToolResult:
    """Adapter for delete_file — delegates to op_runtime file handler.

    Builds FileIROp(op="delete").
    """
    from reyn.core.op_runtime import execute_op
    from reyn.schemas.models import FileIROp

    op = FileIROp(kind="file", op="delete", path=args["path"])
    legacy_ctx = _build_legacy_op_context(ctx)
    return await execute_op(op, legacy_ctx)


async def _handle_edit(args: Mapping[str, Any], ctx: ToolContext) -> ToolResult:
    """Adapter for edit_file — delegates to op_runtime file handler.

    Builds FileIROp(op="edit"). The op_runtime handler enforces:
      * old_string must appear in the file (= 0 matches → error)
      * old_string must be unique unless replace_all=true (= multi-match
        without replace_all → error with count)
      * write permission gating (same tier as write_file / delete_file)
    """
    from reyn.core.op_runtime import execute_op
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
    return await execute_op(op, legacy_ctx)


async def _handle_list(args: Mapping[str, Any], ctx: ToolContext) -> ToolResult:
    """Adapter for list_directory — delegates to op_runtime file handler.

    FileIROp has no `list_directory` op variant; directory listing is
    implemented via op="glob" with a synthesised `<path>/*` pattern.
    This matches the router session._file_list_directory implementation
    exactly (= single canonical approach, no divergence).

    Path normalisation: map "" / "/" / "./" to "." so the LLM's typical
    "list files here" intent resolves to cwd rather than filesystem root.

    `max_results` is forwarded to FileIROp.max_results (default 50) for the
    same reason as _handle_glob below: list_directory synthesises an
    internal glob op, so it shares FileIROp's 50-match cap. That cap is no
    longer a SILENT hole (#2998): when it discards matches, `truncated` is
    forwarded onto this adapter's own result (see below) alongside
    `total_count`/`returned_count`.
    """
    from reyn.core.op_runtime import execute_op
    from reyn.schemas.models import FileIROp

    path = args["path"]
    if path in ("", "/", "./"):
        path = "."

    op = FileIROp(
        kind="file",
        op="glob",
        path=f"{path.rstrip('/')}/*",
        max_results=args.get("max_results", 50),
    )
    legacy_ctx = _build_legacy_op_context(ctx)
    result = await execute_op(op, legacy_ctx)

    # Normalise to {path, entries} shape (= same as session._file_list_directory).
    # Preserve op="glob" + status + matches so file_to_canonical's glob branch
    # fires (#2695: dropping op made the mapper fall through to "None: ok",
    # silently losing the listing). entries is the caller-ergonomic alias;
    # matches is the field the canonical glob branch renders.
    if result.get("status") == "ok":
        matches = result.get("matches", [])
        out: dict = {
            "op": "glob",
            "status": "ok",
            "path": path,
            "entries": matches,
            "matches": matches,
        }
        # #2998: forward the glob op's truncation signal — this adapter only
        # ever hand-picks fields off `result`, so a field not explicitly
        # copied here is silently dropped even though the op_runtime handler
        # set it (the exact silent-loss shape this fix targets).
        if result.get("truncated"):
            out["truncated"] = True
            out["total_count"] = result.get("total_count")
            out["returned_count"] = result.get("returned_count")
        return out
    # #3095: preserve `status` on the error branch too (op_runtime's own
    # denied/error result always carries one). Dropping it here made this
    # adapter's error shape `{"error": ...}` — no `status` key at all — an
    # ASYMMETRIC contract vs. the success shape above (`status: "ok"`
    # always present). A pipeline `tool:` step gates on tool-level failure
    # via `schema: {status: {type: enum, values: ["ok"]}}` (see
    # rag_ingest.yaml's X1 preflight); with `status` missing, that gate
    # silently PASSES a failed call (enum check only fires when the field
    # is present-and-wrong, and this field was absent-not-required), so the
    # failure surfaced later and opaquely wherever the caller consumed the
    # data instead of at the schema gate meant to catch it.
    return {
        "status": result.get("status", "error"),
        "error": result.get("error", "list_directory failed"),
    }


async def _handle_grep(args: Mapping[str, Any], ctx: ToolContext) -> ToolResult:
    """Adapter for grep_files — delegates to op_runtime file handler.

    Maps the router-side `case_sensitive` boolean to the op_runtime
    `case_insensitive` convention (= FileIROp.case_insensitive).
    """
    from reyn.core.op_runtime import execute_op
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
    return await execute_op(op, legacy_ctx)


async def _handle_glob(args: Mapping[str, Any], ctx: ToolContext) -> ToolResult:
    """Adapter for glob_files — delegates to op_runtime file handler.

    Combines `path` (root dir) and `pattern` (glob) into the FileIROp.path
    field that the glob op uses as its glob pattern. The op_runtime glob op
    interprets FileIROp.path as the full glob pattern, so we build
    `<path>/<pattern>` (or just `<pattern>` when path is absent / ".").

    `max_results` is forwarded to FileIROp.max_results (default 50, same as
    the FileIROp field default) — until this fix it was silently dropped:
    the param wasn't in the tool schema and this handler never read it, so
    every glob_files call was hard-capped at 50 matches with no error or
    warning (found via a real pipeline run: 60 files on disk, step passed
    max_results=1000, got exactly 50 back). Callers ingesting a whole
    directory (e.g. RAG folder-indexing) should still pass max_results
    explicitly when more than 50 matches are expected — the default stays
    50 by design — but forgetting to is no longer silent (#2998): the
    op_runtime glob handler now sets `truncated`/`total_count`/
    `returned_count` on the result whenever the cap actually discarded
    matches, which `file_to_canonical` surfaces to the LLM as frontmatter
    meta (and `list_directory`'s router-chat path appends as a trailing
    note — see `_normalise_router_tool_result` in router_loop.py).

    `absolute` (#3102) is forwarded to `FileIROp.absolute` — opt-in, default
    False (unchanged project-relative return for every existing caller). A
    caller that needs an absolute path regardless of whether its OWN
    pattern was relative (e.g. building a `file://` URI) passes
    `absolute: true` explicitly rather than relativizing/re-resolving the
    match itself, which R1 pipelines have no primitive to do.
    """
    from reyn.core.op_runtime import execute_op
    from reyn.schemas.models import FileIROp

    root = args.get("path", ".").rstrip("/")
    pattern = args["pattern"]
    # Combine: if root is "." use pattern directly (avoids "./**/*.py" oddity
    # when workspace.glob_files is cwd-relative). Otherwise prefix the root.
    combined = pattern if root in ("", ".") else f"{root}/{pattern}"
    op = FileIROp(
        kind="file",
        op="glob",
        path=combined,
        max_results=args.get("max_results", 50),
        absolute=bool(args.get("absolute", False)),
    )
    legacy_ctx = _build_legacy_op_context(ctx)
    result = await execute_op(op, legacy_ctx)

    # Normalise: surface as {pattern, matches, count} for caller ergonomics.
    # Preserve op="glob" + status so file_to_canonical's glob branch fires
    # (#2695: dropping op made the mapper fall through to "None: ok", silently
    # losing every match). pattern/matches/count stay as the ergonomic fields.
    if result.get("status") == "ok":
        out: dict = {
            "op": "glob",
            "status": "ok",
            "pattern": combined,
            "matches": result.get("matches", []),
            "count": result.get("count", 0),
        }
        # #2998: forward the op_runtime glob handler's truncation signal — this
        # adapter, like `_handle_list` above, only ever hand-picks fields off
        # `result`, so a field not explicitly copied here is silently dropped
        # even though the handler set it.
        if result.get("truncated"):
            out["truncated"] = True
            out["total_count"] = result.get("total_count")
            out["returned_count"] = result.get("returned_count")
        return out
    # #3095: preserve `status` on the error branch (see the matching comment
    # in `_handle_list` above — same adapter-level bug, same fix). Without
    # it, a glob_files failure (e.g. a permission-denied source directory)
    # produced `{"error": ...}` with no `status` key: a pipeline `tool:`
    # step that gates on `schema: {status: {type: enum, values: ["ok"]}}`
    # (the rag_ingest.yaml X1 preflight pattern) could not catch it (the
    # enum check only fires when the field is present-and-wrong), so the
    # already-declared `for_each: on_error: abort` around this exact call in
    # `rag_ingest.yaml`'s file-discovery step never engaged — the failure
    # instead flowed on as a normal "success" item into a `fold` that
    # assumed every item's `.structured` was a list, and broke on `list +
    # dict` several steps downstream of the real failure (#3095).
    return {
        "status": result.get("status", "error"),
        "error": result.get("error", "glob_files failed"),
    }


from reyn.core.offload.canonical import file_to_canonical  # noqa: E402

READ_FILE = ToolDefinition(
    canonical=file_to_canonical,
    name="read_file",
    router_dispatched=True,
    description=_READ_FILE_DESCRIPTION,
    parameters=_READ_FILE_PARAMETERS,
    gates=ToolGates(router="allow", phase="allow"),
    handler=_handle_read,
    category="io",
    purity="read_only",
)

WRITE_FILE = ToolDefinition(
    canonical=file_to_canonical,
    name="write_file",
    router_dispatched=True,
    description=_WRITE_FILE_DESCRIPTION,
    parameters=_WRITE_FILE_PARAMETERS,
    gates=ToolGates(router="allow", phase="allow"),
    handler=_handle_write,
    category="io",
    purity="side_effect",
)

DELETE_FILE = ToolDefinition(
    canonical=file_to_canonical,
    name="delete_file",
    router_dispatched=True,
    description=_DELETE_FILE_DESCRIPTION,
    parameters=_DELETE_FILE_PARAMETERS,
    gates=ToolGates(router="allow", phase="allow"),
    handler=_handle_delete,
    category="io",
    purity="side_effect",
)

EDIT_FILE = ToolDefinition(
    canonical=file_to_canonical,
    name="edit_file",
    router_dispatched=True,
    description=_EDIT_FILE_DESCRIPTION,
    parameters=_EDIT_FILE_PARAMETERS,
    gates=ToolGates(router="allow", phase="allow"),
    handler=_handle_edit,
    category="io",
    purity="side_effect",
)

LIST_DIRECTORY = ToolDefinition(
    canonical=file_to_canonical,
    name="list_directory",
    router_dispatched=True,
    description=_LIST_DIRECTORY_DESCRIPTION,
    parameters=_LIST_DIRECTORY_PARAMETERS,
    gates=ToolGates(router="allow", phase="allow"),
    handler=_handle_list,
    category="io",
    purity="read_only",
)

GREP_FILES = ToolDefinition(
    canonical=file_to_canonical,
    name="grep_files",
    router_dispatched=True,
    description=_GREP_FILES_DESCRIPTION,
    parameters=_GREP_FILES_PARAMETERS,
    gates=ToolGates(router="allow", phase="allow"),
    handler=_handle_grep,
    category="io",
    purity="read_only",
)

GLOB_FILES = ToolDefinition(
    canonical=file_to_canonical,
    name="glob_files",
    router_dispatched=True,
    description=_GLOB_FILES_DESCRIPTION,
    parameters=_GLOB_FILES_PARAMETERS,
    gates=ToolGates(router="allow", phase="allow"),
    handler=_handle_glob,
    category="io",
    purity="read_only",
)

