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
    "under .reyn/tool-results/. resource_uri: cross-host capable "
    "alternative to path (reyn-tool-result://<agent>/<artifact>); pass "
    "this when the path_ref carries one. Exactly one of path or "
    "resource_uri is required. offset / limit slice by line (0-indexed) "
    "— the same shape as read_file / reyn_src_read / read_memory_body. "
    "max_bytes caps the returned size after slicing (default 16384)."
)

_READ_TOOL_RESULT_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "path": {"type": "string"},
        "resource_uri": {
            "type": "string",
            "description": (
                "Cross-host capable handle "
                "(reyn-tool-result://<agent>/<artifact>). Alternative "
                "to `path` for path-refs that carry a resource_uri. "
                "Same-host resolution today; cross-host RPC support is "
                "tracked under #385 β core impl sub-task 3."
            ),
        },
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
    # Neither path nor resource_uri is in `required` — exactly-one-of is
    # enforced by the handler. JSON Schema oneOf is hard for LLMs to
    # consume reliably; the description carries the semantic rule and
    # the handler returns a structured error if both / neither are given.
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
    resource_uri = str(args.get("resource_uri", "") or "").strip()

    # Exactly-one-of contract (= the schema description). Both / neither
    # surface as structured errors so the LLM can correct without crash.
    if not path and not resource_uri:
        return {
            "status": "error",
            "error": (
                "either path (project-relative under .reyn/tool-results/) "
                "or resource_uri (reyn-tool-result://<agent>/<artifact>) "
                "is required"
            ),
        }
    if path and resource_uri:
        return {
            "status": "error",
            "error": (
                "pass exactly one of path or resource_uri, not both"
            ),
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

    # Same-host fs read for path; resource_uri dispatches through
    # ``read_tool_result_by_uri`` which raises ValueError on cross-host
    # URIs (= sub-task 3 will lift this; today the stub error is the
    # contract). PermissionError covers path-traversal escapes.
    try:
        if resource_uri:
            content, found = legacy_ctx.media_store.read_tool_result_by_uri(
                resource_uri,
            )
        else:
            content, found = legacy_ctx.media_store.read_tool_result(path)
    except (PermissionError, ValueError) as exc:
        return {"status": "error", "error": str(exc)}

    if not found:
        # Echo whichever identifier the LLM supplied so it can correlate
        # the not_found with its prior request without bookkeeping.
        identifier = resource_uri or path
        return {
            "status": "not_found",
            "path": identifier,
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
    # Echo whichever identifier the LLM supplied so it can correlate
    # the response with its prior request without bookkeeping (= same
    # pattern as the not_found branch above).
    identifier = resource_uri or path
    if max_bytes > 0 and total_bytes > max_bytes:
        # Truncate on a UTF-8 boundary by using ``errors="replace"``. The
        # LLM still gets the head of the body; the tail is reachable by
        # repeating the call with a higher ``max_bytes``.
        truncated = encoded[:max_bytes].decode("utf-8", errors="replace")
        return {
            "status": "ok",
            "path": identifier,
            "content": truncated,
            "truncated": True,
            "max_bytes": max_bytes,
            "total_bytes": total_bytes,
        }
    return {
        "status": "ok",
        "path": identifier,
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
