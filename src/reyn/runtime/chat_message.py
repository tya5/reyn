"""ChatMessage — the chat-history entry value object.

One ``ChatMessage`` is a single entry in the LLM-facing conversation history,
shaped to mirror the OpenAI/Anthropic message-list wire format so the history
serialises straight to the LLM (``user`` / ``assistant`` / ``tool`` / ``system``
/ ``summary`` roles; ``str`` or list-of-parts ``content``;
OpenAI tool-turn fields). Also provides the read-time migration that rewrites
pre-#383 on-disk history entries into this shape (``_migrate_legacy_chat_message``)
and the ``_now_iso`` timestamp helper. Pure value object — no dependency on
``Session``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal

# #73: typed (not form-sniffed) tool-outcome classification, stamped on a
# ``role="tool"`` message's ``meta`` at PERSIST time by the ONE place that
# already knows the classification (``router_loop.py``'s tool-result
# assembly — a dispatch-envelope ``{"status":"error",...}`` or an MCP
# ``isError`` result). A consumer (e.g. the TUI restore projection,
# ``interfaces/inline/textual_chat/restore.py``) reads these keys directly —
# it must NEVER re-derive the classification by sniffing the rendered
# ``content`` string (that string's shape is a renderer/display concern, not
# a stable data contract, and a success payload can legitimately start with
# the same words an error message would). ABSENCE of ``TOOL_STATUS_META_KEY``
# (e.g. a pre-#73 persisted history) means "unknown" — a reader must treat
# that as success/completed (today's existing behavior), never infer failure
# from its absence or from the content string.
TOOL_STATUS_META_KEY = "tool_status"
TOOL_STATUS_ERROR = "error"
TOOL_ERROR_KIND_META_KEY = "error_kind"
TOOL_ERROR_MESSAGE_META_KEY = "error_message"


@dataclass(init=False)
class ChatMessage:
    """Chat-history entry, shaped to mirror the OpenAI/Anthropic message
    list wire format (issue #383 E-full).

    Each ``ChatMessage`` is one entry in the LLM-facing conversation, so
    ``self.history`` can be serialised straight to the LLM without
    synthesis. Tool turns are represented as their own ``role="tool"``
    entries; assistant turns that emitted tool calls carry the
    ``tool_calls`` field; multi-modal user / tool turns use the
    list-of-parts ``content`` shape.

    Role vocabulary:
      - ``user`` — user input
      - ``assistant`` — LLM reply (= previously ``agent``)
      - ``tool`` — tool response (= new)
      - ``system`` — system prompt (rare; usually built at wire time)
      - ``summary`` — chat-compactor output (Reyn-internal, filtered at wire boundary)
    """
    role: Literal[
        "user", "assistant", "tool", "system", "summary",
    ]
    # ``content`` is either:
    #   - a ``str`` (= text-only turn), or
    #   - a ``list[dict]`` of litellm-style content parts (= multimodal user
    #     turn / tool response with an image / etc.). Each part is e.g.
    #       {"type": "text", "text": "..."}
    #       {"type": "image_url", "image_url": {"url": "<data url OR file ref>"}}
    #       {"type": "image",     "path": "<abs or cwd-rel>",
    #                             "mime_type": "...", "content_hash": "sha256:..."}
    # The last shape (= ``"image"`` with ``path``) is the **path-ref**
    # introduced by #383: storage points at a file on disk, the
    # wire-shape builder reads and embeds the binary at LLM-call time.
    content: str | list[dict] = ""
    ts: str = ""
    seq: int = 0  # monotonic per-session sequence id; 0 for non-conversational entries
    meta: dict = field(default_factory=dict)
    # OpenAI/Anthropic tool-turn fields ─────────────────────────────────
    # ``tool_calls`` is set ONLY on ``role="assistant"`` entries where the
    # LLM emitted one or more tool calls. Each block follows the OpenAI
    # function-tool shape:
    #   {"id": "<tool_call_id>", "type": "function",
    #    "function": {"name": "<tool>", "arguments": "<json str>"}}
    tool_calls: list[dict] | None = None
    # ``tool_call_id`` is set ONLY on ``role="tool"`` entries. Links the
    # response back to the originating ``tool_call`` block on the
    # preceding assistant message.
    tool_call_id: str | None = None
    # ``name`` is set ONLY on ``role="tool"`` entries (= function name).
    # Mirrors the OpenAI tool-message ``name`` field; some providers
    # require it for tool-result attribution.
    name: str | None = None

    def __init__(
        self,
        role: str,
        content: "str | list[dict]" = "",
        ts: str = "",
        seq: int = 0,
        meta: "dict | None" = None,
        tool_calls: "list[dict] | None" = None,
        tool_call_id: "str | None" = None,
        name: "str | None" = None,
    ) -> None:
        # Reject the pre-#383 ``"agent"`` spelling. Migration of on-disk
        # ``history.jsonl`` entries happens at load time via
        # ``_migrate_legacy_chat_message``; nothing else should be
        # constructing with ``role="agent"`` anymore.
        if role == "agent":
            raise ValueError(
                "ChatMessage role='agent' was renamed to 'assistant' in "
                "issue #383. Pass role='assistant' instead. "
                "(Legacy on-disk entries are migrated read-time by "
                "_migrate_legacy_chat_message.)"
            )
        self.role = role
        self.content = content
        self.ts = ts
        self.seq = seq
        self.meta = meta if meta is not None else {}
        self.tool_calls = tool_calls
        self.tool_call_id = tool_call_id
        self.name = name

    @property
    def text(self) -> str:
        """Derived view returning a str representation of ``content``.

        - str content → returned as-is.
        - list-of-parts content → the first ``{"type":"text"}`` part's text.
        - neither → empty string.

        This is a convenience accessor, NOT a legacy compatibility shim:
        readers that want a textual rendering of any ChatMessage (text or
        multimodal) call ``m.text`` instead of branching on isinstance.
        Writers update ``content`` directly.
        """
        if isinstance(self.content, str):
            return self.content
        if isinstance(self.content, list):
            for part in self.content:
                if isinstance(part, dict) and part.get("type") == "text":
                    return part.get("text", "")
        return ""


# ── Legacy ChatMessage migration ───────────────────────────────────────
#
# history.jsonl files written before issue #383 used the pre-Design-B
# shape: ``role`` ∈ {"user","agent","summary"}; ``text:
# str``; ``media: list[dict]`` (= inline base64 image_url parts from
# #366). On load, ``_migrate_legacy_chat_message`` rewrites such
# entries into the new wire shape so the runtime only ever sees
# Design-B ChatMessage instances.


def _migrate_legacy_chat_message(raw: dict) -> dict:
    """Read-time migration for pre-#383 history.jsonl entries.

    Detects the legacy shape (= ``text`` key + optional ``media`` list,
    ``role="agent"`` for assistant replies) and emits the Design-B
    shape (= ``content`` field, ``role="assistant"``). Mutates a copy;
    the caller hands the result to ``ChatMessage(**kwargs)``.

    Legacy → new:
      role: "agent"            → "assistant"
      text: "hi"               → content: "hi"
      text + media: [...]      → content: [{"type": "text", "text": "hi"}, ...media]
      (no text, media: [...])  → content: [...media]

    Inline base64 in media blocks is left alone — those entries
    pre-date the path-ref design and rewriting them to files would
    be a one-shot tool, out of scope for read-time migration.
    """
    raw = dict(raw)  # don't mutate the caller's dict
    if "content" in raw:
        # Already new shape (= written post-#383 or already migrated).
        # Still normalise role just in case "agent" snuck in.
        if raw.get("role") == "agent":
            raw["role"] = "assistant"
        return raw

    # Legacy shape: text + optional media.
    text_val = raw.pop("text", "")
    media_val = raw.pop("media", None) or []

    if media_val:
        parts: list[dict] = []
        if text_val:
            parts.append({"type": "text", "text": text_val})
        parts.extend(media_val)
        raw["content"] = parts
    else:
        raw["content"] = text_val

    if raw.get("role") == "agent":
        raw["role"] = "assistant"
    return raw


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
