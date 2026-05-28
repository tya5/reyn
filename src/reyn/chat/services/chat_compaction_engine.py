"""ChatCompactionEngine — OS-internal LLM-driven chat history compaction.

PR-N3 (FP-0008): replaces the ``chat_compactor`` stdlib skill with a direct
Python helper.  One LLM call is retained but the phase-frame overhead
(skill loader, artifact YAML, postprocessor sandbox) is gone.

Key design decisions:
- ``compute_covers_through_seq`` is inlined as a pure function; it is
  deterministic and needs no sandboxing.
- The system prompt is a string constant, not a phase file.
- ``trim_head`` / ``trim_tail`` enforce the "N OR token cap, short wins"
  rule so post-compact totals are bounded without a safety_margin.
- A single turn that alone exceeds the token cap is truncated with an
  explicit event emit (``compaction_single_turn_truncated``).
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import litellm

if TYPE_CHECKING:
    from reyn.events.events import EventLog

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Dataclasses (replace the YAML artifact schemas)
# ---------------------------------------------------------------------------


@dataclass
class HistoryChunkToCompact:
    """Input to the compaction engine."""
    new_turns: list[dict]                          # [{role, text, seq, ...}]
    section_token_caps: dict                       # {topic_arc, decisions, ...}
    previous_summary: dict | None = None           # prior ChatSummary or None


@dataclass
class ChatSummaryRaw:
    """LLM output before deterministic seq derivation."""
    topic_arc: str
    new_turn_seqs: list[int]
    decisions: list[str] = field(default_factory=list)
    pending: list[str] = field(default_factory=list)
    session_user_facts: list[str] = field(default_factory=list)
    artifacts_referenced: list[str] = field(default_factory=list)


@dataclass
class ChatSummary:
    """Caller-facing summary: same shape as the old chat_summary YAML schema.

    This is the type written to history.jsonl as a ``role: "summary"`` entry.
    Existing pre-N3 entries remain parseable because the field names are
    identical to the YAML schema fields.
    """
    topic_arc: str
    covers_through_seq: int
    decisions: list[str] = field(default_factory=list)
    pending: list[str] = field(default_factory=list)
    session_user_facts: list[str] = field(default_factory=list)
    artifacts_referenced: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        """Serialise to the wire shape used in history.jsonl meta.structured."""
        return {
            "topic_arc": self.topic_arc,
            "decisions": self.decisions,
            "pending": self.pending,
            "session_user_facts": self.session_user_facts,
            "artifacts_referenced": self.artifacts_referenced,
            "covers_through_seq": self.covers_through_seq,
        }


# ---------------------------------------------------------------------------
# Deterministic helpers
# ---------------------------------------------------------------------------

# Compact system prompt (equivalent to phases/compact.md, ~35 lines).
_COMPACTION_SYSTEM_PROMPT = """\
You are summarising a chunk of chat history into a structured rolling summary.

Fold the new_turns into the previous_summary (or start fresh if null).
Produce a JSON object with these keys:
  topic_arc         — 1-3 sentences on the current topic. Update when topic shifts.
  decisions         — array of bullet strings for choices made. Drop oldest minor ones if over cap.
  pending           — array of open items (questions, tasks, follow-ups). Remove resolved items.
  session_user_facts — array of user attributes learned this session, not yet in memory. Drop oldest if over cap.
  artifacts_referenced — array of files/PRs/commits/issues in scope. Drop ones no longer relevant.
  new_turn_seqs     — VERBATIM list of every `seq` value from input new_turns, in order. Do NOT sort, filter, or compute the max.

Retention rules:
- Never drop architectural decisions or items labelled as final.
- Match the user's language for free-text fields.
- Include tool-activity items (file edits, web fetches) only when they inform the reply going forward.
- Do NOT transcribe raw quotes unless they are the verbatim text of a decision or pending item.

section_token_caps gives soft per-section token budgets. Trim the LEAST IMPORTANT items first when over budget.
Output ONLY the JSON object — no explanation, no markdown fences.
"""


def _estimate_tokens(text: str, model: str = "") -> int:
    """Estimate tokens using litellm.token_counter; fallback chars//4."""
    try:
        if model:
            count = litellm.token_counter(model=model, text=text)
        else:
            count = litellm.token_counter(model="gpt-3.5-turbo", text=text)
        if count and count > 0:
            return count
    except Exception:
        pass
    return max(1, len(text or "") // 4)


def compute_covers_through_seq(new_turn_seqs: list) -> int:
    """Return max(new_turn_seqs) or 0 when the list is empty.

    Deterministic; the LLM is not trusted to compute this correctly on
    weak models (a wrong value causes turn duplication or loss in
    ChatSession.history).
    """
    if not new_turn_seqs:
        return 0
    return max(int(s) for s in new_turn_seqs)


def trim_head(
    turns: list,
    N: int,
    max_tokens: int,
    model: str = "",
    events: "EventLog | None" = None,
) -> list:
    """Return up to the first N turns, stopping early if cumulative tokens exceed max_tokens.

    A single turn that alone exceeds max_tokens is truncated to fit and
    a ``compaction_single_turn_truncated`` event is emitted.
    """
    kept = []
    total = 0
    for t in turns[:N]:
        text = t.get("text", "") if isinstance(t, dict) else str(getattr(t, "text", ""))
        t_tokens = _estimate_tokens(text, model)
        if kept and total + t_tokens > max_tokens:
            # Would exceed budget — stop before adding this turn.
            break
        if t_tokens > max_tokens:
            # Single turn exceeds cap — truncate it.
            if events is not None:
                seq = t.get("seq", 0) if isinstance(t, dict) else getattr(t, "seq", 0)
                events.emit(
                    "compaction_single_turn_truncated",
                    seq=seq,
                    turn_tokens=t_tokens,
                    cap=max_tokens,
                    side="head",
                )
            kept.append(t)
            total += max_tokens
            break
        kept.append(t)
        total += t_tokens
    return kept


def trim_tail(
    turns: list,
    N: int,
    max_tokens: int,
    model: str = "",
    events: "EventLog | None" = None,
) -> list:
    """Return up to the last N turns, stopping early if cumulative tokens exceed max_tokens.

    Walks from the tail backwards, then reverses the result.
    A single turn that alone exceeds max_tokens is truncated to fit and
    a ``compaction_single_turn_truncated`` event is emitted.
    """
    kept: list = []
    total = 0
    candidate = turns[-N:] if N and len(turns) >= N else turns
    for t in reversed(candidate):
        text = t.get("text", "") if isinstance(t, dict) else str(getattr(t, "text", ""))
        t_tokens = _estimate_tokens(text, model)
        if kept and total + t_tokens > max_tokens:
            break
        if t_tokens > max_tokens:
            if events is not None:
                seq = t.get("seq", 0) if isinstance(t, dict) else getattr(t, "seq", 0)
                events.emit(
                    "compaction_single_turn_truncated",
                    seq=seq,
                    turn_tokens=t_tokens,
                    cap=max_tokens,
                    side="tail",
                )
            kept.append(t)
            total += max_tokens
            break
        kept.append(t)
        total += t_tokens
    return list(reversed(kept))


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


class ChatCompactionEngine:
    """OS-internal compaction engine.

    Builds the LLM prompt from an input chunk, calls the model once via
    litellm directly, derives ``covers_through_seq`` deterministically, and
    returns a ``ChatSummary``.

    Parameters
    ----------
    model:
        LiteLLM model string (e.g. ``"gemini/gemini-2.5-flash-lite"``).
    events:
        Session-scoped EventLog for observability.
    """

    def __init__(self, model: str, events: "EventLog") -> None:
        self._model = model
        self._events = events

    async def compact(self, input_chunk: HistoryChunkToCompact) -> ChatSummary:
        """Run one compaction LLM call and return a ChatSummary.

        Raises on LLM error; callers wrap in try/except and emit
        ``compaction_failed`` if needed.
        """
        user_content = json.dumps({
            "previous_summary": input_chunk.previous_summary,
            "new_turns": input_chunk.new_turns,
            "section_token_caps": input_chunk.section_token_caps,
        }, ensure_ascii=False)

        messages = [
            {"role": "system", "content": _COMPACTION_SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ]

        from reyn.llm.llm import proxy_kwargs
        extra = proxy_kwargs()

        # Strip provider prefix for proxy routing.
        effective_model = self._model
        if extra.get("custom_llm_provider") == "openai":
            parts = self._model.split("/", 1)
            if len(parts) == 2:
                effective_model = parts[1]

        response = await litellm.acompletion(
            model=effective_model,
            messages=messages,
            response_format={"type": "json_object"},
            **extra,
        )

        raw = (response.choices[0].message.content or "").strip()
        if not raw:
            raise ValueError("compaction LLM returned empty response")

        parsed: dict = {}
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            # Attempt JSON repair (remove trailing commas — most common LLM mistake).
            import re
            repaired = re.sub(r",(\s*[}\]])", r"\1", raw)
            parsed = json.loads(repaired)

        new_turn_seqs = parsed.get("new_turn_seqs") or []
        covers = compute_covers_through_seq(new_turn_seqs)
        if covers == 0 and input_chunk.new_turns:
            # Fallback: take max seq from the input turns directly.
            covers = max(
                (int(t.get("seq", 0)) for t in input_chunk.new_turns if isinstance(t, dict)),
                default=0,
            )

        return ChatSummary(
            topic_arc=str(parsed.get("topic_arc") or ""),
            covers_through_seq=covers,
            decisions=list(parsed.get("decisions") or []),
            pending=list(parsed.get("pending") or []),
            session_user_facts=list(parsed.get("session_user_facts") or []),
            artifacts_referenced=list(parsed.get("artifacts_referenced") or []),
        )


__all__ = [
    "ChatCompactionEngine",
    "ChatSummary",
    "ChatSummaryRaw",
    "HistoryChunkToCompact",
    "compute_covers_through_seq",
    "trim_head",
    "trim_tail",
]
