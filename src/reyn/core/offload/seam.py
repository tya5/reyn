"""The single offload seam — build a tool-result body from a canonical result (#2425 案B step1b).

Given a :class:`~reyn.core.offload.canonical.CanonicalToolResult`, produce the inline tool-message
body the caller caps, plus the media blocks for the caller's vision follow-up. The crux of 案B lives
here: ``text`` is the SOLE offload payload; ``structured`` / ``media`` are attachments handled OUT of
the text-offload decision, so a large ``structured`` can NEVER become a second oversized field that
collapses the offload to a whole-dict JSON envelope (the owner chat-MCP whole-envelope bug).

- **media** attachments → returned as raw blocks for the caller's existing vision follow-up (byte
  path unchanged; this replaces the per-op ``media_blocks`` extraction at the chat chokepoint).
- **structured** attachments → kept as data when small; when large, offloaded to their OWN ref via
  ``save_fn`` (preserved + retrievable, not dropped, not competing with ``text``). This also folds in
  the tui-found miss (``structured`` used to slip past the media-strip and reach the offload decision).
- **text** → the body's payload; the caller caps ``json.dumps(body)`` with ``payload_field="text"`` so
  the offloaded ref holds the text CLEAN (real newlines), never a whole-dict envelope.

Store-parameterized (``save_fn``) so the same seam serves chat (``MediaStore.save_tool_result``) and,
if the phase path is retained, the phase axis — but only the chat integration is wired now (#2425
re-scope: the phase-side integration is on hold pending the phase-deletion decision).
"""
from __future__ import annotations

import json
from typing import Any, Callable

from reyn.core.offload.canonical import CanonicalToolResult

# A structured attachment larger than this (serialized chars) is offloaded to its own ref rather than
# kept inline — so it never bloats the body or competes with ``text`` in the offload decision.
STRUCTURED_INLINE_MAX_CHARS: int = 2_000
_STRUCTURED_PREVIEW_CHARS: int = 600


def build_offload_body(
    canonical: CanonicalToolResult,
    *,
    save_fn: Callable[..., dict],
) -> tuple[dict, list[dict]]:
    """Return ``(body, media_blocks)`` for a canonical tool result.

    ``body`` = ``{**meta, "text": text[, "attachments": [<small structured | structured refs>]]}`` —
    the caller serialises + caps it with ``payload_field="text"``. ``media_blocks`` = the raw media
    blocks for the caller's vision follow-up. Large structured attachments are offloaded to their own
    ref via ``save_fn`` (preserved, non-competing)."""
    media_blocks: list[dict] = []
    structured_inline: list[dict] = []
    for att in canonical.get("attachments", []) or []:
        kind = att.get("kind")
        if kind == "media":
            block = att.get("block")
            if isinstance(block, dict):
                media_blocks.append(block)
        elif kind == "structured":
            data = att.get("data")
            serialized = json.dumps(data, ensure_ascii=False, default=str)
            if len(serialized) > STRUCTURED_INLINE_MAX_CHARS:
                stored = save_fn(serialized)
                structured_inline.append({
                    "kind": "structured",
                    "_offload_ref": stored.get("path", ""),
                    "_offload_content_hash": stored.get("content_hash", ""),
                    "_offload_total_chars": len(serialized),
                    "_offload_preview": serialized[:_STRUCTURED_PREVIEW_CHARS],
                })
            else:
                structured_inline.append({"kind": "structured", "data": data})

    body: dict[str, Any] = {**(canonical.get("meta") or {}), "text": canonical.get("text", "")}
    if structured_inline:
        body["attachments"] = structured_inline
    return body, media_blocks
