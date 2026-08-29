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

# #5364 §1.2: the tool-result history-content resolver's own persisted
# signals — typed via named meta keys, NOT a new top-level ChatMessage
# field (lead-coder ruling: a new field creates a "missing" state for
# every ALREADY-persisted record, the same defect class ``seq: int = 0``'s
# own "0 = no coordinate assigned (pre-fix history only)" already carries;
# `meta` + a named key is this repo's typed convention for exactly this
# shape — see `TOOL_STATUS_META_KEY` above, "restore.py reads this typed
# field directly, matching reyn's typed-over-form-sniffed convention").
#
# ABSENCE of ``SPILLED_META_KEY`` (pre-#5364 history) means "never
# spilled" (today's only possible history — nothing offloaded a tool
# result into this store before #5364 existed), never "unknown".
SPILLED_META_KEY = "spilled"
# The backing file's project-relative path — set for every SPILLED entry.
# #5364 §1.1 "A": an offload attempt is ALWAYS file-backed when it lands —
# a SPILLED entry's own persisted content is the ref rather than the
# original inline body, so only a spilled entry needs this to resolve.
# #5364 §1.5: "A" is not "always" without exception — a write that is
# known, in advance, not to land (MediaStoreWriteUnavailable) never
# reaches this store at all; that turn's content stays inline and this
# key is never set (see LOST_REASON_NEVER_PERSISTED below).
CONTENT_REF_META_KEY = "content_ref"
# Set once `resolve()` (reyn.core.offload.history_content_resolve) has
# actually observed the backing file missing — never guessed ahead of
# that check. ABSENCE means "not (yet) known to be lost", never "present".
LOST_META_KEY = "lost"
LOST_REASON_META_KEY = "lost_reason"
# #5364 §1.5: the two possible reasons, as constants (lead-coder review:
# a bare "gc"/"never_persisted" string written at more than one call site
# lets one side's typo pass silently — the same discipline
# ``TOOL_STATUS_ERROR`` above already applies to its own value domain).
LOST_REASON_GC = "gc"
LOST_REASON_NEVER_PERSISTED = "never_persisted"

# #3299 P4: the intervention PROMPT + resolved ANSWER, stamped on the
# ``role="user"`` history entry ``InterventionHandler.deliver_answer_to``
# already appends (mirroring ``intervention_id`` / ``intervention_kind``
# alongside it). ``InterventionHandler.announce`` never writes to history —
# it only publishes to the outbox — so before this, the QUESTION half of an
# answered intervention did not exist anywhere in ``history.jsonl``; the TUI
# restore projection (``interfaces/inline/textual_chat/restore.py``) could
# not show it after a restart. Rather than inventing a correlation key to
# join a separate prompt record (there is no such record, and P5's
# out-of-order answering makes any GUESSED key a repeat of the #3287/#3299 P2
# "guessed correlation key" defect class), the prompt is folded onto the
# SAME answer record — one history entry is now fully self-contained, no
# correlation needed at all.
#
# ★Untrusted / RAW (#2770 discipline: "the single truth is RAW, neutralize at
# each display boundary"): ``ask_user`` prompts/suggestions come straight
# from a model tool-call, and a selected CHOICE's label is one of those
# model-supplied options too. These three values are stored EXACTLY as
# ``UserIntervention`` carried them (no neutralization at write time) — a
# consumer (restore projection, any future surface) MUST neutralize before
# rendering, never persist a display-shaped (already-neutralized) copy, or
# the audit/restore record stops being the original. The live TUI path's
# equivalent leaf (``intervention_handler._neutralize_terminal`` /
# ``presenter._neutralized_label``) neutralizes at ITS OWN render call site,
# not at persist time — this mirrors that discipline for the restore path.
#
# These NEVER reach the LLM: ``RouterHistoryBuffer._serialise_turn`` builds
# the wire dict from ``role`` / ``content`` / ``tool_calls`` / ``tool_call_id``
# / ``name`` (+ the ``reasoning`` meta sub-key) only — arbitrary ``meta`` keys
# (these three included) are never copied into the payload. So this addition
# costs zero LLM context / tokens; it only grows the PERSISTED
# ``history.jsonl`` (something that was already visible via the outbox
# ``announce`` — this makes it durable, not newly exposed).
INTERVENTION_PROMPT_META_KEY = "intervention_prompt"
INTERVENTION_DETAIL_META_KEY = "intervention_detail"
#: The resolved answer's DISPLAY text — a matched CHOICE's ``label`` (model-
#: supplied, RAW/untrusted) or the raw free-text answer. Needed because a
#: choice-selected answer's own ``ChatMessage.content`` is an EMPTY string
#: (``InterventionHandler.deliver_answer_to`` passes ``text=""`` through the
#: choice-id-override path — the choice id, not a label, is what the wire
#: transport carries) — so ``m.text`` alone cannot reconstruct "what was
#: answered" for a closed-set intervention; this key always carries it.
INTERVENTION_ANSWER_META_KEY = "intervention_answer"

# #3629: stamped on a ``role="tool"`` ``load_skill`` result's persisted entry
# ONLY (``router_loop.py``'s tool-result assembly, mirroring the
# ``TOOL_STATUS_META_KEY`` pattern above — the ONE place that already knows
# a mapper set ``history_text``/``history_meta``, canonical.py's
# ``load_skill_to_canonical``). ``content`` for such an entry keeps
# ``${REYN_SKILL_DIR}``/``${REYN_PLUGIN_ROOT}`` (+ ``CLAUDE_*`` aliases)
# LITERAL rather than baked to an absolute value that a later rename/move
# would freeze forever (history is immutable) — these two keys are what a
# wire-serialise pass (``router_history_buffer.py``'s ``_serialise_turn`` →
# ``reyn.plugins.skill_load.refresh_location_tokens``) needs to re-resolve
# the tokens FRESH, against the CURRENT filesystem, every time the entry is
# replayed.
#
# ``TOKEN_MAP_META_KEY`` is audit-completeness ONLY (#3629 architect
# ruling: LLM-payload trace dumping is opt-in via ``REYN_LLM_TRACE_DUMP``,
# so history is the only ALWAYS-ON record of what a turn's tokens actually
# resolved to at the time) — a wire-serialise pass MUST NOT read substitution
# VALUES from it; it re-derives fresh values from ``SKILL_SOURCE_PATH_META_KEY``
# every time (a frozen value can only repeat what was already stale; only a
# re-resolvable identity can self-heal — see ``refresh_location_tokens``'s
# docstring). Like every other ``meta`` key, this NEVER reaches the LLM
# (``RouterHistoryBuffer._serialise_turn`` builds the wire dict from
# ``role``/``content``/``tool_calls``/``tool_call_id``/``name`` only).
TOKEN_MAP_META_KEY = "token_map"
SKILL_SOURCE_PATH_META_KEY = "skill_source_path"


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
      - ``summary`` — chat-compactor output (Reyn-internal; ``build_history``'s
        own projection still filters it out and attaches its content via a
        synthetic bridge turn instead — but ``RouterHistoryBuffer.
        decompose_history_for_retry``'s projection (#5531) includes it
        directly, positioned by ordinary chronological order like any
        other turn, not filtered)
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
    seq: int = 0  # monotonic per-session sequence id; #3704: 0 = no coordinate assigned (pre-fix history only — every entry now gets one at persist time, any role)
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
