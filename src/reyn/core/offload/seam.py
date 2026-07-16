"""The single offload seam — build the LLM-visible tool-result body from a canonical result (#2425 案B).

Given a :class:`~reyn.core.offload.canonical.CanonicalToolResult`, produce the ``role: tool`` message
content the LLM sees, plus the media blocks for the caller's vision follow-up. The crux of 案B lives
here: ``text`` is the SOLE text-offload payload; ``structured`` / ``media`` are attachments handled OUT
of the text-offload decision, so a large ``structured`` can NEVER become a second oversized field that
collapses the offload to a whole-dict JSON envelope (the owner chat-MCP whole-envelope bug).

LLM-visible format (frontmatter + text, no JSON envelope):

- no structured/signal-meta → the plain ``text`` string (no wrapper);
- structured data or signal meta present → a YAML frontmatter block, then the text body::

      ---
      <yaml: signal meta + structured data | structured ref+preview>
      ---
      <text>

- error → a plain string, never JSON: ``Error (<kind>): <message>`` (dispatch envelope) or
  ``Error: <text>`` (MCP ``isError``). Success and error are syntactically distinguishable with no
  status field.

Two independent offload streams:

- **text** — capped by ``cap_tool_result_content`` (token budget) at the caller; its preview is plain
  text, not a JSON stub.
- **structured** — gated here by ``STRUCTURED_INLINE_MAX_CHARS`` (its own ref when oversized); the
  frontmatter then carries ``structured_ref`` + a short ``structured_preview`` instead of the data.

The format is INDEPENDENT of the store: with no ``save_fn`` the frontmatter still renders (structured
stays inline, uncapped) — only the size-gated offloading needs a store. Media attachments are returned
raw for the existing vision follow-up, never embedded in the text body.
"""
from __future__ import annotations

import json
from typing import Any, Callable

from reyn.core.offload.canonical import CanonicalToolResult

# A structured attachment larger than this (serialized chars) is offloaded to its own ref rather than
# kept inline — so it never bloats the frontmatter or competes with ``text`` in the offload decision.
STRUCTURED_INLINE_MAX_CHARS: int = 2_000
_STRUCTURED_PREVIEW_CHARS: int = 600


def build_offload_body(
    canonical: CanonicalToolResult,
    *,
    save_fn: "Callable[..., dict] | None" = None,
    enabled: bool = True,
) -> tuple[dict, str, list[dict], "str | None"]:
    """Return ``(frontmatter, text, media_blocks, content_type)`` for a canonical tool result.

    ``frontmatter`` = the signal meta + the structured data (inline when small, or ``structured_ref`` +
    ``structured_preview`` when large and a ``save_fn`` is available). Empty ``{}`` when there is
    nothing but plain text. ``text`` = the (uncapped) body the caller then caps + assembles via
    :func:`render_tool_result`. ``media_blocks`` = the raw media blocks for the vision follow-up.
    ``content_type`` (#2663) = the canonical's RENDERER-only sidecar (``canonical.get("content_type")``)
    — deliberately NEVER folded into ``frontmatter`` (that would leak a transport/renderer signal into
    the LLM-visible ``role: tool`` body); the caller threads it to the ``text`` offload store's
    ``mime_type`` instead, so present's stage-3 default viewer can later recover it from the stored
    ref's file extension (the store already does exactly this for images — #385).

    ``save_fn`` may be ``None`` (no media store): the format still applies — an oversized structured
    attachment is kept INLINE (it cannot be offloaded without a store) rather than dropped.

    ``enabled=False`` (tool-result-schema-redesign §5 debug lever) disables the
    ``STRUCTURED_INLINE_MAX_CHARS`` size gate — structured data always stays inline
    regardless of size, never offloaded to a ``structured_ref``."""
    media_blocks: list[dict] = []
    structured_items: list[Any] = []
    for att in canonical.get("attachments", []) or []:
        kind = att.get("kind")
        if kind == "media":
            block = att.get("block")
            if isinstance(block, dict):
                media_blocks.append(block)
        elif kind == "structured":
            structured_items.append(att.get("data"))

    # Signal meta goes to the frontmatter as-is (``isError`` is handled by the error path, never here).
    # ``empty`` (#3010) deliberately DOES render: it is an ordinary signal, the same class as
    # ``returncode``/``truncated``, and telling the LLM outright that a success produced no body is
    # the same "better UX than a blank result" reasoning the explicit marker itself rests on. It
    # ADDS a frontmatter line; it never rewrites ``text``, which stays the marker byte-for-byte.
    frontmatter: dict[str, Any] = {
        k: v for k, v in (canonical.get("meta") or {}).items() if k != "isError"
    }

    if structured_items:
        combined: Any = structured_items[0] if len(structured_items) == 1 else structured_items
        serialized = json.dumps(combined, ensure_ascii=False, default=str)
        if enabled and len(serialized) > STRUCTURED_INLINE_MAX_CHARS and save_fn is not None:
            # ``tool="structured"`` distinguishes the structured stream's filename from the text
            # stream's (the caller's text cap also stores through ``save_fn`` with the default tool
            # token) — otherwise both would collide on the same-second filename and one would clobber
            # the other, losing a stream.
            stored = save_fn(serialized, tool="structured")
            frontmatter["structured"] = "offloaded"
            frontmatter["structured_ref"] = stored.get("path", "")
            frontmatter["structured_preview"] = serialized[:_STRUCTURED_PREVIEW_CHARS]
        else:
            frontmatter["structured"] = combined

    return frontmatter, canonical.get("text", "") or "", media_blocks, canonical.get("content_type")


def render_tool_result(frontmatter: dict, text: str) -> str:
    """Assemble the LLM-visible ``role: tool`` content from a frontmatter dict and the (already-capped)
    text body.

    - ``frontmatter`` non-empty → ``---\\n<yaml>---\\n<text>`` (``default_flow_style=False``,
      ``allow_unicode=True``, keys unsorted for readability).
    - ``frontmatter`` empty → the plain ``text``. Edge guard: if ``text`` itself starts with ``---``
      it is prefixed with a blank line so it cannot be misparsed as a frontmatter block.
    """
    if frontmatter:
        import yaml

        block = yaml.dump(
            frontmatter, default_flow_style=False, allow_unicode=True, sort_keys=False,
        )
        return f"---\n{block}---\n{text}"
    if text.startswith("---"):
        return "\n" + text
    return text
