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

from reyn.services.compaction.engine import _IMAGE_FIXED_TOKEN_COST
from reyn.tools.types import ToolContext, ToolDefinition, ToolGates, ToolResult

_READ_TOOL_RESULT_DESCRIPTION = (
    "PATH SCOPE: ONLY for tool_result_ref resources under "
    ".reyn/tool-results/ (= artifact refs from prior tool calls). "
    "NEVER for regular source / project files — use file__read or "
    "invoke_action(reyn.source__read) for those instead. "
    "Read a tool_result_ref resource — the full content referenced by "
    "a path_ref preview. Use when the preview does not contain the "
    "content needed for what comes next. Exactly ONE of path / "
    "resource_uri / url is required. path: project-relative path "
    "under .reyn/tool-results/ (= same-host only). resource_uri: "
    "vendor-scheme identifier (reyn-tool-result://<agent>/<artifact>; "
    "= same-host only). url: standard HTTPS URL — fetched via HTTP "
    "GET when the host differs from local, short-circuited to fs read "
    "when it matches. offset / limit slice by line (0-indexed) — the "
    "same shape as read_file / reyn_src_read / read_memory_body. "
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
                "Same-host resolution only; for cross-host fetch use "
                "the `url` field instead."
            ),
        },
        "url": {
            "type": "string",
            "description": (
                "Standard HTTPS URL from the path_ref's `url` field "
                "(= cross-host capable). When the URL host matches "
                "the local Reyn instance, the read short-circuits to "
                "fs; when it differs, the handler HTTP-GETs the body."
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


# #272 media axis load-contract: image refs are not text-loadable. The per-image
# prompt cost is single-sourced from services/compaction/engine._IMAGE_FIXED_TOKEN_COST
# (one constant, no drift across the 3 sites) — what the LLM needs to know is the
# context cost, not the on-disk byte size. Name preserved for in-module use.
_MEDIA_REF_IMAGE_TOKEN_COST = _IMAGE_FIXED_TOKEN_COST
_IMAGE_REF_EXTENSIONS = (
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff", ".tif", ".svg",
)


def _is_image_ref(identifier: str) -> bool:
    """True when *identifier* (path / url / resource_uri) names an image file.

    Detection is extension-based on the final path segment — sufficient because
    media refs are minted by MediaStore.save_* with the mime-derived extension.
    """
    tail = identifier.rstrip("/").rsplit("/", 1)[-1].split("?", 1)[0].lower()
    return tail.endswith(_IMAGE_REF_EXTENSIONS)


def _source_agent_from_uri(resource_uri: str) -> str | None:
    """Extract the source_agent from a resource_uri for event tagging."""
    from reyn.workspace.media_store import parse_resource_uri
    parsed = parse_resource_uri(resource_uri)
    return parsed[0] if parsed else None


def _source_agent_from_url(url: str) -> str | None:
    """Extract the source_agent from a path_ref ``url`` (= the 2nd
    segment of the path: ``/agents/<agent>/tool-results/<artifact>``).

    Returns None when the URL doesn't match the expected shape — the
    handler then surfaces ``source_agent="unknown"`` in the event so
    measurement still sees the URL-based read, just without provenance.
    """
    from urllib.parse import urlparse
    parsed = urlparse(url)
    segments = parsed.path.strip("/").split("/")
    if len(segments) >= 4 and segments[0] == "agents" and segments[2] == "tool-results":
        return segments[1] or None
    return None


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


async def _http_get_body(
    url: str, *, timeout: float = 30.0,
) -> tuple[str, int, str | None]:
    """HTTP GET the URL's body for cross-host ``read_tool_result``
    dispatch (= #385 β core impl sub-task 3c).

    Returns ``(body, status_code, error_message)``:
      - on 200: ``(body, 200, None)``
      - on non-200: ``("", status_code, error_message)``
      - on transport failure (= connection / timeout): ``("", 0, error_message)``

    Body is decoded as UTF-8 (= text/plain tool results); binary
    bodies (= image artifacts) currently fall through to the same
    decoding, which may produce replacement characters — that's
    acceptable because LLM consumers of ``read_tool_result`` are
    text-focused (= image consumption goes through ``read_image`` /
    materialise paths).

    The function is async and uses ``httpx.AsyncClient`` so the
    handler stays async-friendly. Tests can substitute via the
    ``_HTTP_GET_OVERRIDE`` module-level hook below.
    """
    if _HTTP_GET_OVERRIDE is not None:
        return await _HTTP_GET_OVERRIDE(url, timeout=timeout)
    try:
        import httpx
    except ImportError:
        return "", 0, (
            "httpx not installed; cross-host read_tool_result requires "
            "the [web] extra. install with: pip install -e .[web]"
        )
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(url)
    except Exception as exc:  # noqa: BLE001 — surface any transport failure
        return "", 0, f"HTTP GET failed: {type(exc).__name__}: {exc}"
    if resp.status_code != 200:
        return "", resp.status_code, (
            f"HTTP {resp.status_code} from {url}"
        )
    return resp.text, 200, None


# Test override hook: set to an async callable matching
# ``async (url, *, timeout) -> (body, status, error)`` to substitute
# for the real httpx call. Used by ``tests/test_read_tool_result_tool.py``
# cross-host tests; production code path is the real httpx.
_HTTP_GET_OVERRIDE: Any = None


async def _fetch_via_url(
    media_store: Any, url: str,
) -> tuple[str, bool, str | None]:
    """Resolve a path-ref ``url`` via the appropriate transport.

    Returns ``(body, found, http_error)``:
      - same-host short-circuit succeeded: ``(body, True, None)``
      - same-host file missing: ``("", False, None)``
      - cross-host HTTP GET succeeded: ``(body, True, None)``
      - cross-host HTTP non-200 / transport failure: ``("", False, error_message)``

    The dispatcher first asks ``MediaStore.read_tool_result_by_url``
    whether the URL is local. ValueError from that call means "not
    local" — fall through to HTTP GET. Other exceptions surface.
    """
    try:
        body, found = media_store.read_tool_result_by_url(url)
        return body, found, None
    except ValueError:
        # Not local — fall through to HTTP GET.
        pass
    body, status, http_error = await _http_get_body(url)
    if http_error is not None:
        if status == 404:
            # Map 404 to the "not_found" status path the handler already
            # returns for missing same-host files (= unified consumer
            # contract across transports).
            return "", False, None
        return "", False, http_error
    return body, True, None


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
    url = str(args.get("url", "") or "").strip()

    # Identifier kind derivation (= used by every emit call below).
    # Exactly-one-of contract across three identifier kinds.
    supplied = [k for k, v in (
        ("path", path), ("resource_uri", resource_uri), ("url", url),
    ) if v]
    if len(supplied) == 1:
        identifier_kind = supplied[0]
    elif len(supplied) == 0:
        identifier_kind = "missing"
    else:
        identifier_kind = "multiple"  # caller violation, validated below
    identifier = url or resource_uri or path

    # Exactly-one-of contract (= the schema description). Both / neither
    # surface as structured errors so the LLM can correct without crash.
    if not supplied:
        err = (
            "exactly one of path, resource_uri, or url is required "
            "(path: project-relative under .reyn/tool-results/; "
            "resource_uri: reyn-tool-result://<agent>/<artifact>; "
            "url: https://.../agents/<agent>/tool-results/<artifact>)"
        )
        _emit_event(
            ctx, status="error", error_kind="missing_args",
            identifier_kind="missing", identifier="", error=err,
        )
        return {"status": "error", "error": err}
    if len(supplied) > 1:
        err = (
            "pass exactly one of path, resource_uri, or url, not "
            f"multiple (supplied: {', '.join(supplied)})"
        )
        _emit_event(
            ctx, status="error", error_kind="both_supplied",
            identifier_kind="multiple", identifier=identifier, error=err,
        )
        return {"status": "error", "error": err}

    rs = ctx.router_state
    if rs is not None and rs.op_context_factory is not None:
        legacy_ctx = rs.op_context_factory()
    else:
        # Propagate the active phase's PermissionDecl via
        # phase_state.op_context (FP-0008 Tool→OpContext bridge fix
        # 2026-05-28).
        phase_op_ctx = (
            ctx.phase_state.op_context if ctx.phase_state is not None else None
        )
        legacy_ctx = OpContext(
            workspace=ctx.workspace,
            events=ctx.events,
            permission_decl=(
                phase_op_ctx.permission_decl
                if phase_op_ctx is not None
                else PermissionDecl()
            ),
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

    # #272 media-axis load-contract (never ref→ref, never overflow): an image
    # ref is NOT text-loadable here. Reading its raw bytes as text would return
    # binary garbage and — if large — overflow the prompt; returning another ref
    # would loop. So return a SMALL structured error carrying the context cost
    # (never the binary, never a ref). The image itself re-enters the
    # conversation through the per-turn-bounded media follow-up (#272 Part-1),
    # not as text — this keeps media structurally unable to overflow the prompt.
    if _is_image_ref(identifier):
        err = (
            f"'{identifier}' is an image ref — not loadable as text via "
            f"read_tool_result (~{_MEDIA_REF_IMAGE_TOKEN_COST} tokens of image "
            f"data in context). Images re-enter the conversation through the "
            f"bounded media follow-up, not as text; do not inline the raw bytes."
        )
        _emit_event(
            ctx, status="error", error_kind="media_not_text_loadable",
            identifier_kind=identifier_kind, identifier=identifier, error=err,
            media_size_tokens=_MEDIA_REF_IMAGE_TOKEN_COST,
        )
        return {
            "status": "error",
            "error_kind": "media_not_text_loadable",
            "error": err,
            "media_size_tokens": _MEDIA_REF_IMAGE_TOKEN_COST,
        }

    # Dispatch by identifier kind (= #385 β core impl sub-task 3c).
    #
    # ``path``         : same-host fs only (= the original Phase 1 shape)
    # ``resource_uri`` : same-host fs via vendor-scheme dispatcher; cross-
    #                    host URIs surface ``cross_host_stub`` error_kind
    #                    (= sub-task 3d MCP adapter would lift this)
    # ``url``          : new sub-task 3c path. Local URL → short-circuit
    #                    to fs read; remote URL → HTTP GET via httpx.
    #
    # PermissionError covers path-traversal escapes (= MediaStore
    # boundary check); ValueError covers all dispatcher-side
    # validation (= invalid URI / cross-host stub / etc.).
    try:
        if url:
            content, found, http_error = await _fetch_via_url(
                legacy_ctx.media_store, url,
            )
            if http_error is not None:
                _emit_event(
                    ctx, status="error", error_kind="http_error",
                    identifier_kind=identifier_kind, identifier=identifier,
                    error=http_error,
                )
                return {"status": "error", "error": http_error}
        elif resource_uri:
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
    # source_agent extraction for the event payload. Path-based reads
    # always source from local. resource_uri carries the agent in the
    # URI segment. url carries it in the path component (= 2nd segment
    # of /agents/<agent>/tool-results/<artifact>). Cross-host vs same-
    # host doesn't change this field (= it's about the producer's
    # identity, not where the read happened to go).
    if resource_uri:
        source_agent = _source_agent_from_uri(resource_uri) or "unknown"
    elif url:
        source_agent = _source_agent_from_url(url) or "unknown"
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
