"""read_tool_result ToolDefinition — companion to #385 PoC preview-driven
tool returns.

When a tool (= web_fetch as of #385 PoC PR-D, eventually file_read / grep
once the pattern generalises) saves its full content under
``.reyn/tool-results/`` and returns a path-ref + preview instead of the
raw body, the LLM can call ``read_tool_result(path=...)`` if it decides
the preview is insufficient. This is the lazy-expand half of the
preview-driven design: preview-by-default keeps content out of context
(= 改変 noise + cost win), and this tool is the explicit opt-in to pull
the full body in when the preview can't answer the question.

Path validation lives in :meth:`MediaStore.read_tool_result` — the
resolved path must be inside ``tool_results_dir`` (= workspace
boundary), otherwise the call raises ``PermissionError``.

Slice arguments (= ``offset`` / ``limit``) follow PR #409's line-based
contract shared with ``read_file`` / ``reyn_src_read`` / ``read_memory_body``
so the four "read one entry" surfaces stay parameter-symmetric. ``offset``
counts 0-indexed lines from the start of the body; ``limit`` caps the
line count taken. Slice happens before ``max_bytes`` truncation so the
byte cap applies to the resulting sliced content (= two orthogonal axes
for partial-read shape control).

Out of scope (= follow-up work):

  - search-within-result (= ``grep_tool_result(path, pattern)``).
  - cleanup policy enforcement (= LLM cannot delete; user-managed).
  - cross-host RPC routing via ``resource_uri`` (= #385 β core impl,
    gated on Step 2 measurement success path confirmation).
"""
from __future__ import annotations

from typing import Any, Mapping

from reyn.tools.types import ToolContext, ToolDefinition, ToolGates, ToolResult

_READ_TOOL_RESULT_DESCRIPTION = (
    "Read a tool_result_ref resource — the full content referenced by "
    "a path_ref preview. Use when the preview does not contain the "
    "content needed for what comes next. path: project-relative path "
    "under .reyn/tool-results/. offset / limit slice by line (0-indexed) "
    "— the same shape as read_file / reyn_src_read / read_memory_body. "
    "max_bytes caps the returned size after slicing (default 16384)."
)

_READ_TOOL_RESULT_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "path": {"type": "string"},
        "offset": {
            "type": "integer",
            "description": (
                "Line number to start reading from (0-indexed). "
                "Omit to start at the beginning of the body."
            ),
        },
        "limit": {
            "type": "integer",
            "description": (
                "Number of lines to read from `offset`. "
                "Omit to read through end of body."
            ),
        },
        "max_bytes": {"type": "integer"},
    },
    "required": ["path"],
}


async def _handle(args: Mapping[str, Any], ctx: ToolContext) -> ToolResult:
    """Read a tool-result file by project-relative path.

    Builds a legacy ``OpContext`` to access ``media_store`` (= same
    bridging pattern as ``web_fetch._handle``). When ``media_store`` is
    not configured (= legacy / non-multimodal session), surfaces a
    structured error rather than crashing.
    """
    from reyn.op_runtime.context import OpContext
    from reyn.permissions.permissions import PermissionDecl

    path = str(args.get("path", "") or "").strip()
    if not path:
        return {
            "status": "error",
            "error": "path is required (project-relative under .reyn/tool-results/)",
        }

    rs = ctx.router_state
    if rs is not None and rs.op_context_factory is not None:
        legacy_ctx = rs.op_context_factory()
    else:
        legacy_ctx = OpContext(
            workspace=ctx.workspace,
            events=ctx.events,
            permission_decl=PermissionDecl(),
            permission_resolver=ctx.permission_resolver,
            skill_name="",
            subscribers=getattr(ctx.events, "subscribers", []),
        )

    if legacy_ctx.media_store is None:
        return {
            "status": "error",
            "error": (
                "MediaStore is not configured for this session. "
                "Tool-result expansion requires the multimodal media "
                "storage layer (= reyn.local.yaml multimodal section)."
            ),
        }

    try:
        content, found = legacy_ctx.media_store.read_tool_result(path)
    except PermissionError as exc:
        return {"status": "error", "error": str(exc)}

    if not found:
        return {
            "status": "not_found",
            "path": path,
            "error": "tool result file does not exist or was deleted",
        }

    offset_raw = args.get("offset")
    limit_raw = args.get("limit")
    try:
        offset = int(offset_raw) if offset_raw is not None else None
    except (TypeError, ValueError):
        offset = None
    try:
        limit = int(limit_raw) if limit_raw is not None else None
    except (TypeError, ValueError):
        limit = None

    if offset is not None or limit is not None:
        lines = content.splitlines(keepends=True)
        start = max(0, offset or 0)
        sliced = lines[start:start + limit] if limit is not None else lines[start:]
        content = "".join(sliced)

    max_bytes_raw = args.get("max_bytes")
    try:
        max_bytes = int(max_bytes_raw) if max_bytes_raw is not None else 16384
    except (TypeError, ValueError):
        max_bytes = 16384

    encoded = content.encode("utf-8")
    total_bytes = len(encoded)
    if max_bytes > 0 and total_bytes > max_bytes:
        # Truncate on a UTF-8 boundary by using ``errors="replace"``. The
        # LLM still gets the head of the body; the tail is reachable by
        # repeating the call with a higher ``max_bytes``.
        truncated = encoded[:max_bytes].decode("utf-8", errors="replace")
        return {
            "status": "ok",
            "path": path,
            "content": truncated,
            "truncated": True,
            "max_bytes": max_bytes,
            "total_bytes": total_bytes,
        }
    return {
        "status": "ok",
        "path": path,
        "content": content,
        "truncated": False,
        "total_bytes": total_bytes,
    }


READ_TOOL_RESULT = ToolDefinition(
    name="read_tool_result",
    description=_READ_TOOL_RESULT_DESCRIPTION,
    parameters=_READ_TOOL_RESULT_PARAMETERS,
    gates=ToolGates(router="allow", phase="allow"),
    handler=_handle,
    category="io",
    purity="read_only",
)
