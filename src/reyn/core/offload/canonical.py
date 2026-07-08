"""Canonical tool-result shape — the #2425 案B spec fix (guessing-free offload).

The spec hole (owner): tool results are heterogeneous per tool, so the offload layer GUESSED which
field is the LLM body (per-op ``_offload_payload_field`` marker + ``decide_payload_field``'s
sole-oversized rule) and broke whenever a result had a second large field (owner's chat-MCP
whole-envelope: a large ``structuredContent`` alongside ``content``).

案B normalizes every tool result at the boundary into ONE canonical shape the offloader never has to
interpret:

- ``text``        — the single canonical LLM-readable body (the ONLY thing the offloader truncates).
- ``attachments`` — typed non-text kept OUT of the offload decision so a large one never competes with
                    ``text``: ``{"kind": "media"|"structured", ...}``. A large ``structured`` is
                    separately referenced (not dropped, not mixed into ``text``, retrievable by ref).
- ``source_ref``  — the re-fetch origin for an on-disk body (``{"path": …, "offset": …}``); ``None``
                    for a transient body (MCP/web/exec) — which must therefore be offload-stored (the
                    tool cannot be meaningfully re-run for more).
- ``meta``        — small structured signal the LLM reads inline as YAML frontmatter (``returncode``,
                    ``truncated``, …). High-signal-only: transport identifiers (``kind``, duplicate
                    ``status``, ``server``, ``tool`` echo) are dropped — they never change what the
                    LLM does next. ``isError`` is retained as the sole error-path driver.

Every op kind that once declared ``_offload_payload_field`` now has a mapper here (案B endgame, not a
partial migration): MCP + web fetch/search + sandboxed exec + recall/index_query + run_pipeline(_async).
An unregistered kind falls back to a whole-dict ``structured`` attachment (never lost, and
``ctx.<name>.structured.<field>`` still gives programmatic access). The chat offload path no longer
guesses a payload field: the six per-op ``_offload_payload_field`` markers and the chat use of
``decide_payload_field`` / the sole-oversized condition are gone — ``text`` is the payload by
construction. (``decide_payload_field`` / ``_oversized_fields`` themselves remain only for the now-dead
control_ir/phase offloader, pending its separate removal.)

P7: the op-kind → canonical mapping is OS-level (no domain-specific vocabulary). The deref / paging / offload
store machinery is reused unchanged — 案B removes only the field-guessing.
"""
from __future__ import annotations

from typing import Any, TypedDict


class CanonicalToolResult(TypedDict, total=False):
    """The single shape all tool results are normalized to before offload (see module docstring)."""

    text: str
    attachments: list[dict]
    source_ref: "dict | None"
    meta: dict


def _is_error(result: dict) -> bool:
    """A result is an error when it explicitly flags one (MCP ``isError``) or its op-level
    ``status`` is ``error`` (the sole error-path driver kept after meta-tightening)."""
    return bool(result.get("isError")) or result.get("status") == "error"


def _mcp_to_canonical(result: dict) -> CanonicalToolResult:
    """MCP result → canonical. ``content`` (joined text) → ``text``; ``structured``
    (structuredContent) + ``media_blocks`` → typed ``attachments`` (a large ``structured`` is
    separately referenced, never competing with ``text`` in the offload decision — the owner
    whole-envelope root). ``meta`` is tightened to signal-only: the transport echo
    (``kind``/``status``/``server``/``tool``) is dropped; only ``isError`` survives, as the error-path
    driver. source_ref is None (transient: an MCP call can't be meaningfully re-run for more, so its
    body is offload-stored)."""
    attachments: list[dict] = []
    structured = result.get("structured")
    if structured is not None:
        attachments.append({"kind": "structured", "data": structured})
    for block in result.get("media_blocks") or []:
        attachments.append({"kind": "media", "block": block})
    meta = {"isError": True} if _is_error(result) else {}
    return CanonicalToolResult(
        text=result.get("content", "") or "",
        attachments=attachments,
        source_ref=None,
        meta=meta,
    )


def _mcp_read_resource_to_canonical(result: dict) -> CanonicalToolResult:
    """MCP resource-read result → canonical (#2597 slice ②a). ``contents`` is a list of flattened
    ``TextResourceContents``/``BlobResourceContents``: a ``text`` entry joins into the canonical
    ``text``; a ``blob`` entry (base64, arbitrary mimeType) becomes a ``structured`` attachment rather
    than a ``media`` one (the existing ``media`` path assumes vision-follow-up image blocks). A large
    blob is size-gated to its own ref so it never competes with ``text``. ``meta`` is signal-only
    (``isError`` when error)."""
    attachments: list[dict] = []
    text_parts: list[str] = []
    for item in result.get("contents") or []:
        if not isinstance(item, dict):
            continue
        if "text" in item and item["text"] is not None:
            text_parts.append(str(item["text"]))
        elif "blob" in item:
            attachments.append({"kind": "structured", "data": item})
    meta = {"isError": True} if _is_error(result) else {}
    return CanonicalToolResult(
        text="\n".join(text_parts),
        attachments=attachments,
        source_ref=None,
        meta=meta,
    )


def _mcp_get_prompt_to_canonical(result: dict) -> CanonicalToolResult:
    """MCP get-prompt result → canonical (#2597 slice ②c). ``messages`` is a list of flattened
    ``PromptMessage`` dicts; each message's text content joins into the canonical ``text``; a non-text
    content block (image/audio/embedded-resource) becomes a ``structured`` attachment (kept out of the
    text-offload decision, same as its two siblings). ``meta`` is signal-only (``isError`` when
    error)."""
    attachments: list[dict] = []
    text_parts: list[str] = []
    for message in result.get("messages") or []:
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if isinstance(content, dict) and content.get("type") == "text" and content.get("text") is not None:
            text_parts.append(str(content["text"]))
        elif content is not None:
            attachments.append({"kind": "structured", "data": content})
    meta = {"isError": True} if _is_error(result) else {}
    return CanonicalToolResult(
        text="\n".join(text_parts),
        attachments=attachments,
        source_ref=None,
        meta=meta,
    )


def _web_fetch_to_canonical(result: dict) -> CanonicalToolResult:
    """web_fetch result → canonical. The fetched page text (``content``, or the op's own ``preview``
    when it pre-offloaded to a ``path_ref``) → ``text``. Signal meta: ``truncated`` + ``next_start``
    (the LLM's pagination handle) when the fetch was cut. Transport (url/status/content_type/extractor)
    is dropped."""
    content = result.get("content")
    text = content if content else str(result.get("preview") or "")
    meta: dict[str, Any] = {}
    if result.get("truncated"):
        meta["truncated"] = True
        next_start = result.get("next_start")
        if next_start is not None:
            meta["next_start"] = next_start
    return CanonicalToolResult(text=text, attachments=[], source_ref=None, meta=meta)


def _web_search_to_canonical(result: dict) -> CanonicalToolResult:
    """web_search result → canonical. The ``results`` list → a ``structured`` attachment (rendered as
    frontmatter YAML, or offloaded to its own ref when large). An op-level failure (no ``results``,
    ``status: error``) surfaces its message as ``text``. Transport (query/backend) is dropped."""
    results = result.get("results")
    if results is not None:
        return CanonicalToolResult(
            text="", attachments=[{"kind": "structured", "data": results}], source_ref=None, meta={},
        )
    text = str(result.get("error") or "") if result.get("status") == "error" else ""
    return CanonicalToolResult(text=text, attachments=[], source_ref=None, meta={})


def _sandboxed_exec_to_canonical(result: dict) -> CanonicalToolResult:
    """sandboxed_exec result → canonical. ``stdout`` (+ ``stderr`` when present) → ``text``; a NONZERO
    ``returncode`` → signal meta (it changes what the LLM does next — a zero code is not signal).
    Transport (backend/truncated) is dropped."""
    stdout = result.get("stdout") or ""
    stderr = result.get("stderr") or ""
    if stderr:
        text = f"{stdout}\n{stderr}" if stdout else stderr
    else:
        text = stdout
    meta: dict[str, Any] = {}
    returncode = result.get("returncode")
    if returncode:  # nonzero (or truthy) only — a 0 exit is not actionable signal
        meta["returncode"] = returncode
    return CanonicalToolResult(text=text, attachments=[], source_ref=None, meta=meta)


def _chunks_to_canonical(result: dict) -> CanonicalToolResult:
    """recall / index_query result → canonical. The retrieved ``chunks`` list → a ``structured``
    attachment (frontmatter YAML, or its own ref when large). There is no text body. Transport
    (``mode``) is dropped."""
    chunks = result.get("chunks")
    attachments = [{"kind": "structured", "data": chunks}] if chunks is not None else []
    return CanonicalToolResult(text="", attachments=attachments, source_ref=None, meta={})


def _run_pipeline_to_canonical(result: dict) -> CanonicalToolResult:
    """Sync run_pipeline result → canonical. The final ``output`` is the whole thing the calling LLM
    wants: a str output → ``text``; a non-str output → a ``structured`` attachment. ``run_id`` and
    ``named_stores`` are correlation/transport plumbing the caller never acts on → dropped (owner
    ruling)."""
    output = result.get("output")
    if isinstance(output, str):
        return CanonicalToolResult(text=output, attachments=[], source_ref=None, meta={})
    attachments = [{"kind": "structured", "data": output}] if output is not None else []
    return CanonicalToolResult(text="", attachments=attachments, source_ref=None, meta={})


def _run_pipeline_async_to_canonical(result: dict) -> CanonicalToolResult:
    """Async run_pipeline result → canonical. Unlike the sync case, ``run_id`` is KEPT — it is the
    correlation handle the caller uses to match the later ``[pipeline]`` completion message."""
    run_id = result.get("run_id")
    text = (
        f"Pipeline started (run_id: {run_id}). "
        f"Result will arrive as a [pipeline] message."
    )
    return CanonicalToolResult(text=text, attachments=[], source_ref=None, meta={})


# op-kind → canonical mapper. Every op that once declared ``_offload_payload_field`` is registered
# here; an unregistered kind falls back to a whole-dict ``structured`` attachment (see ``to_canonical``).
_MAPPERS: dict[str, Any] = {
    "mcp": _mcp_to_canonical,
    "mcp_read_resource": _mcp_read_resource_to_canonical,
    "mcp_get_prompt": _mcp_get_prompt_to_canonical,
    "web_fetch": _web_fetch_to_canonical,
    "web_search": _web_search_to_canonical,
    "sandboxed_exec": _sandboxed_exec_to_canonical,
    "recall": _chunks_to_canonical,
    "index_query": _chunks_to_canonical,
    "run_pipeline": _run_pipeline_to_canonical,
    "run_pipeline_async": _run_pipeline_async_to_canonical,
}


def to_canonical(result: dict) -> CanonicalToolResult:
    """Normalize an op result dict to :class:`CanonicalToolResult`. Dispatches on ``result["kind"]``;
    an unregistered kind falls back to a whole-dict ``structured`` attachment (``text`` empty) so
    nothing is ever lost — the whole dict renders as readable frontmatter YAML and
    ``ctx.<name>.structured.<field>`` still gives programmatic access."""
    kind = result.get("kind")
    mapper = _MAPPERS.get(kind) if isinstance(kind, str) else None
    if mapper is not None:
        return mapper(result)
    # Fallback: not-yet-migrated kind → the whole dict is a structured attachment (lossless, readable
    # as frontmatter YAML), never a whole-dict JSON-into-text blob.
    return CanonicalToolResult(
        text="",
        attachments=[{"kind": "structured", "data": result}],
        source_ref=None,
        meta={},
    )


def unwrap_dispatch_envelope(result: Any) -> Any:
    """Peel any ``{"status": ..., "data": {...}}`` dispatch envelope(s) off a raw tool-dispatch
    result, stopping at the first dict that already carries a ``kind`` (the shape :func:`to_canonical`
    dispatches on). A tool-registry handler's own return value can itself be an envelope (e.g.
    ``run_pipeline``), so more than one layer may need peeling — hence the loop, not a single unwrap."""
    inner = result
    while (
        isinstance(inner, dict)
        and isinstance(inner.get("data"), dict)
        and set(inner) <= {"status", "data", "error"}
        and "kind" not in inner
    ):
        inner = inner["data"]
    return inner


def canonical_to_ctx_fields(canonical: CanonicalToolResult) -> "dict[str, Any]":
    """Reduce a :class:`CanonicalToolResult` to the flat ``{"text": ..., "structured": ...}`` shape a
    pipeline step's ``ctx.<name>`` exposes (``structured`` key absent when there is no structured
    attachment) — shape-only, mirroring ``seam.py``'s attachments reduction but with NO size gating:
    pipeline ctx retains full values for downstream programmatic step processing (owner ruling)."""
    fields: dict[str, Any] = {"text": canonical.get("text", "")}
    structured_items = [
        att.get("data") for att in canonical.get("attachments", []) or [] if att.get("kind") == "structured"
    ]
    if structured_items:
        fields["structured"] = structured_items[0] if len(structured_items) == 1 else structured_items
    return fields
