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

Integrity verification (= #385 β core impl sub-task 4): when the LLM
supplies ``content_hash`` (= the value from the path_ref it received),
the handler hashes the FULL body (= before slice / truncate) with
SHA-256 and compares to the expected. Mismatch surfaces as
``status="error"`` with ``error_kind="hash_mismatch"`` — covers both
"file mutated since path_ref was minted" (= same-host) and "transport
corruption" (= future cross-host RPC). Omit ``content_hash`` to skip
verify (= backward compat for callers that don't carry the hash).

Out of scope (= follow-up work):

  - search-within-result (= ``grep_tool_result(path, pattern)``).
  - cleanup policy enforcement (= LLM cannot delete; user-managed).
  - cross-host RPC routing via ``resource_uri`` (= #385 β core impl
    sub-task 3, gated on the cross-host sequencing thread).
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
    "max_bytes caps the returned size after slicing (default 16384). "
    "content_hash: optional SHA-256 from the path_ref; when supplied, "
    "the read body's hash is verified and a mismatch returns an error."
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
        "content_hash": {
            "type": "string",
            "description": (
                "Optional SHA-256 to verify body integrity. Accepted "
                "formats: 'sha256:<hex>' (= the path_ref's exact form) "
                "or just '<hex>'. When supplied, the read body's hash "
                "is compared to this value; mismatch returns status="
                "error with error_kind=hash_mismatch. Omit to skip "
                "verification (= backward compat for callers without "
                "the hash)."
            ),
        },
    },
    # Neither path nor resource_uri is in `required` — exactly-one-of is
    # enforced by the handler. JSON Schema oneOf is hard for LLMs to
    # consume reliably; the description carries the semantic rule and
    # the handler returns a structured error if both / neither are given.
}


def _emit_event(ctx: ToolContext, **fields: Any) -> None:
    """Emit a ``tool_result_read`` observability event, defensively.

    Observability must not crash the handler — wrap in try/except so a
    misconfigured / null events log can't break tool dispatch. The event
    payload follows the #385 β sub-task 2 schema:

    Required:
      ``status``         — "ok" | "not_found" | "error"
      ``identifier``     — the path or resource_uri the LLM supplied
                           (empty when validation failed before either)
      ``identifier_kind``— "path" | "resource_uri" | "missing"

    On status="ok":
      ``source_agent``   — extracted from resource_uri (= dispatcher target);
                           "local" when the read went through ``path``
      ``total_bytes``    — full body byte size before max_bytes cap
      ``returned_bytes`` — bytes actually included in the response
      ``sliced``         — True when offset/limit were applied
      ``truncated``      — True when max_bytes truncation fired

    On status="error":
      ``error_kind``     — "missing_args" | "both_supplied" |
                           "media_store_unconfigured" | "invalid_uri" |
                           "cross_host_stub" | "path_traversal" |
                           "hash_mismatch" | "other"
      ``error``          — the message surfaced to the LLM
      ``expected_hash`` / ``actual_hash`` — present only when
                           error_kind="hash_mismatch", normalised to the
                           "sha256:<hex>" form
    """
    try:
        ctx.events.emit("tool_result_read", **fields)
    except Exception:
        pass


def _source_agent_from_uri(resource_uri: str) -> str | None:
    """Extract the source_agent from a resource_uri for event tagging."""
    from reyn.workspace.media_store import parse_resource_uri
    parsed = parse_resource_uri(resource_uri)
    return parsed[0] if parsed else None


def _normalise_hash(value: str) -> str:
    """Return ``sha256:<hex>`` form, accepting either prefixed or bare hex.

    Comparisons in :func:`_handle` happen against the normalised form so
    callers can pass the path_ref's exact ``content_hash`` value (=
    ``"sha256:<hex>"``) or just the hex without the prefix.
    """
    value = value.strip().lower()
    if value.startswith("sha256:"):
        return value
    return f"sha256:{value}"


async def _handle(args: Mapping[str, Any], ctx: ToolContext) -> ToolResult:
    """Read a tool-result file by project-relative path or resource URI.

    Builds a legacy ``OpContext`` to access ``media_store`` (= same
    bridging pattern as ``web_fetch._handle``). When ``media_store`` is
    not configured (= legacy / non-multimodal session), surfaces a
    structured error rather than crashing.

    Emits a ``tool_result_read`` event on every dispatch outcome (= ok /
    not_found / each error kind) so observability + #385 measurement
    can count expand frequency by identifier kind and source agent
    without having to scrape the response surface.
    """
    from reyn.op_runtime.context import OpContext
    from reyn.permissions.permissions import PermissionDecl

    path = str(args.get("path", "") or "").strip()
    resource_uri = str(args.get("resource_uri", "") or "").strip()

    # Identifier kind derivation (= used by every emit call below).
    if resource_uri and not path:
        identifier_kind = "resource_uri"
    elif path and not resource_uri:
        identifier_kind = "path"
    elif path and resource_uri:
        identifier_kind = "both"  # caller violation, validated below
    else:
        identifier_kind = "missing"
    identifier = resource_uri or path

    # Exactly-one-of contract (= the schema description). Both / neither
    # surface as structured errors so the LLM can correct without crash.
    if not path and not resource_uri:
        err = (
            "either path (project-relative under .reyn/tool-results/) "
            "or resource_uri (reyn-tool-result://<agent>/<artifact>) "
            "is required"
        )
        _emit_event(
            ctx, status="error", error_kind="missing_args",
            identifier_kind="missing", identifier="", error=err,
        )
        return {"status": "error", "error": err}
    if path and resource_uri:
        err = "pass exactly one of path or resource_uri, not both"
        _emit_event(
            ctx, status="error", error_kind="both_supplied",
            identifier_kind="both", identifier=identifier, error=err,
        )
        return {"status": "error", "error": err}

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
        err = (
            "MediaStore is not configured for this session. "
            "Tool-result expansion requires the multimodal media "
            "storage layer (= reyn.local.yaml multimodal section)."
        )
        _emit_event(
            ctx, status="error", error_kind="media_store_unconfigured",
            identifier_kind=identifier_kind, identifier=identifier, error=err,
        )
        return {"status": "error", "error": err}

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
    except PermissionError as exc:
        _emit_event(
            ctx, status="error", error_kind="path_traversal",
            identifier_kind=identifier_kind, identifier=identifier,
            error=str(exc),
        )
        return {"status": "error", "error": str(exc)}
    except ValueError as exc:
        # Heuristic kind classification from the message — keeps the
        # event payload actionable for measurement (= "how many cross-
        # host attempts vs malformed URIs?") without needing dispatcher-
        # side error types.
        msg = str(exc)
        kind = "cross_host_stub" if "cross-host" in msg else "invalid_uri"
        _emit_event(
            ctx, status="error", error_kind=kind,
            identifier_kind=identifier_kind, identifier=identifier,
            error=msg,
        )
        return {"status": "error", "error": msg}

    if not found:
        _emit_event(
            ctx, status="not_found",
            identifier_kind=identifier_kind, identifier=identifier,
        )
        return {
            "status": "not_found",
            "path": identifier,
            "error": "tool result file does not exist or was deleted",
        }

    # Integrity verify (= #385 β core impl sub-task 4). Hash is computed
    # against the FULL body (= before slice / truncate) so the
    # comparison matches the path_ref's content_hash exactly. When the
    # LLM omits content_hash, verification is skipped (= backward
    # compat). When supplied, mismatch returns hash_mismatch with both
    # expected and actual hashes so the LLM can diagnose (= file
    # mutated after path_ref was minted, transport corruption, etc.).
    expected_hash_raw = str(args.get("content_hash", "") or "").strip()
    if expected_hash_raw:
        import hashlib
        actual_hash = "sha256:" + hashlib.sha256(
            content.encode("utf-8"),
        ).hexdigest()
        expected_hash = _normalise_hash(expected_hash_raw)
        if actual_hash != expected_hash:
            err = (
                f"content_hash mismatch: expected {expected_hash}, "
                f"got {actual_hash}"
            )
            _emit_event(
                ctx, status="error", error_kind="hash_mismatch",
                identifier_kind=identifier_kind, identifier=identifier,
                expected_hash=expected_hash, actual_hash=actual_hash,
                error=err,
            )
            return {
                "status": "error",
                "error": err,
                "expected_hash": expected_hash,
                "actual_hash": actual_hash,
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
    # Identifier is already populated above (= echoed in not_found and
    # event emissions); reuse the same value for the success response.
    sliced = offset is not None or limit is not None
    # source_agent extraction: cross-host fields exist on the path-ref only
    # when the producing MediaStore was constructed with agent_name; when
    # the LLM passed path (not resource_uri), the read went through the
    # local store, so source = "local".
    if resource_uri:
        source_agent = _source_agent_from_uri(resource_uri) or "unknown"
    else:
        source_agent = "local"

    if max_bytes > 0 and total_bytes > max_bytes:
        # Truncate on a UTF-8 boundary by using ``errors="replace"``. The
        # LLM still gets the head of the body; the tail is reachable by
        # repeating the call with a higher ``max_bytes``.
        truncated_str = encoded[:max_bytes].decode("utf-8", errors="replace")
        _emit_event(
            ctx, status="ok",
            identifier_kind=identifier_kind, identifier=identifier,
            source_agent=source_agent,
            total_bytes=total_bytes,
            returned_bytes=len(truncated_str.encode("utf-8")),
            sliced=sliced, truncated=True,
        )
        return {
            "status": "ok",
            "path": identifier,
            "content": truncated_str,
            "truncated": True,
            "max_bytes": max_bytes,
            "total_bytes": total_bytes,
        }
    _emit_event(
        ctx, status="ok",
        identifier_kind=identifier_kind, identifier=identifier,
        source_agent=source_agent,
        total_bytes=total_bytes,
        returned_bytes=total_bytes,
        sliced=sliced, truncated=False,
    )
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
