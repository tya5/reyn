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
- ``meta``        — small structured status the LLM reads inline (``status``, ``server``/``url``,
                    ``returncode``, …).

Each op maps its dict → canonical ONCE, at its boundary (``to_canonical``). ``decide_payload_field``,
``_oversized_fields``, the sole-oversized condition, and the six per-op markers then disappear —
``text`` is the payload by construction. #2417's file_read truncate is this rule for the file case.

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


def _mcp_to_canonical(result: dict) -> CanonicalToolResult:
    """MCP result → canonical. ``content`` (joined text) → ``text``; ``structured``
    (structuredContent) + ``media_blocks`` → typed ``attachments`` (a large ``structured`` is
    separately referenced, never competing with ``text`` in the offload decision — the owner
    whole-envelope root); ``status`` / ``server`` / ``tool`` / ``isError`` → ``meta``. source_ref is
    None (transient: an MCP call can't be meaningfully re-run for more, so its body is offload-stored).
    Forward-complete: every field is assigned somewhere (nothing dropped)."""
    attachments: list[dict] = []
    structured = result.get("structured")
    if structured is not None:
        attachments.append({"kind": "structured", "data": structured})
    for block in result.get("media_blocks") or []:
        attachments.append({"kind": "media", "block": block})
    meta = {
        k: v for k, v in result.items()
        if k in ("kind", "status", "server", "tool", "isError", "error")
    }
    return CanonicalToolResult(
        text=result.get("content", "") or "",
        attachments=attachments,
        source_ref=None,
        meta=meta,
    )


def _mcp_read_resource_to_canonical(result: dict) -> CanonicalToolResult:
    """MCP resource-read result → canonical (#2597 slice ②a). ``contents`` is a
    list of flattened ``TextResourceContents``/``BlobResourceContents``: a
    ``text`` entry joins into the canonical ``text`` (same offload-safe path
    as a tool's joined ``content``); a ``blob`` entry (base64, arbitrary
    mimeType — not necessarily an image) becomes a ``structured`` attachment
    rather than a ``media`` one, since the existing ``media`` attachment path
    assumes vision-follow-up-shaped image blocks that an arbitrary resource
    blob does not fit. ``structured`` attachments are still size-gated
    (:data:`~reyn.core.offload.seam.STRUCTURED_INLINE_MAX_CHARS`) — a large
    blob is offloaded to its own ref rather than competing with ``text``, the
    same guarantee ``_mcp_to_canonical`` gives a tool's ``structuredContent``.
    """
    attachments: list[dict] = []
    text_parts: list[str] = []
    for item in result.get("contents") or []:
        if not isinstance(item, dict):
            continue
        if "text" in item and item["text"] is not None:
            text_parts.append(str(item["text"]))
        elif "blob" in item:
            attachments.append({"kind": "structured", "data": item})
    meta = {
        k: v for k, v in result.items()
        if k in ("kind", "status", "server", "uri", "error")
    }
    return CanonicalToolResult(
        text="\n".join(text_parts),
        attachments=attachments,
        source_ref=None,
        meta=meta,
    )


# op-kind → canonical mapper. New ops register here (or return canonical directly); a raw dict with no
# registered mapper falls back to a whole-dict wrap so nothing is ever lost (see ``to_canonical``).
_MAPPERS: dict[str, Any] = {
    "mcp": _mcp_to_canonical,
    "mcp_read_resource": _mcp_read_resource_to_canonical,
}


def to_canonical(result: dict) -> CanonicalToolResult:
    """Normalize an op result dict to :class:`CanonicalToolResult`. Dispatches on ``result["kind"]``;
    an unregistered kind falls back to a whole-dict wrap (``text`` = the JSON, no attachments/source)
    so migration is incremental and lossless — an op is canonicalized by adding its mapper, and until
    then it round-trips through the fallback unchanged in spirit."""
    kind = result.get("kind")
    mapper = _MAPPERS.get(kind) if isinstance(kind, str) else None
    if mapper is not None:
        return mapper(result)
    # Fallback: not yet migrated. Keep the whole dict as the body (lossless); meta carries the kind.
    import json
    return CanonicalToolResult(
        text=json.dumps(result, ensure_ascii=False),
        attachments=[],
        source_ref=None,
        meta={"kind": kind} if kind is not None else {},
    )
