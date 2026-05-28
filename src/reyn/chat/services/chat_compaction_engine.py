"""ChatCompactionEngine — OS-internal LLM-driven chat history compaction.

PR-N3 (FP-0008, 11-axis): replaces the ``chat_compactor`` stdlib skill with a
direct Python helper.  One LLM call is retained but the phase-frame overhead
(skill loader, artifact YAML, postprocessor sandbox) is gone.

Key design decisions:
- ``compute_covers_through_seq`` is inlined as a pure function; it is
  deterministic and needs no sandboxing.
- The system prompt is a string constant, not a phase file.
  ``T_comp_SP`` is measured once at engine init (independent of the main
  session SP — the main session pool does NOT include T_comp_SP).
- ``trim_head`` / ``trim_tail`` operate purely on token budget, no turn count
  cap (Axis 3).
- A single turn that alone exceeds the token cap is truncated with an
  explicit event emit ``turn_too_large_truncated`` (Axis 7).
- ``estimate_tokens_for_turn`` is multimodal-aware: str content uses
  litellm.token_counter; list[dict] content passes the parts list directly
  or sums per-part text + fixed cost per image (Axis 6).
- All token estimation uses litellm.token_counter by default; opts out to
  chars//4 when ``use_chars4_estimate=True`` (Axis 10).
- ``hard_truncate_summary`` post-processes the LLM's body output so that
  the stored summary is deterministically ≤ body_budget tokens (Axis 9).
- ``NewMsgExceedsBudgetError`` is raised (never silently truncated) when the
  incoming user message exceeds its budget (Axis 11).
- ``compute_budgets`` / ``assert_static_bounds`` enforce the ratio invariants
  at engine init time so a misconfigured reyn.yaml fails fast (Axis 3 derived).
- An ``asyncio.Lock`` on compaction prevents concurrent history appends from
  racing with an in-flight force_compact_now() call (Axis 8).

Drop priority when over budget:
  1. body  — compaction summarises naturally
  2. head  — trim_head enforces token budget
  3. tail  — trim_tail enforces token budget
  4. SP    — dynamic SP truncate is OUT OF SCOPE for PR-N3 (separate wave)
  5. new_msg — NEVER dropped; abort + event emit (see Axis 11)
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import litellm

if TYPE_CHECKING:
    from reyn.config import CompactionConfig
    from reyn.events.events import EventLog

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Token-counter fallback tracking (Axis 10)
# ---------------------------------------------------------------------------

_token_counter_fallback_warned: bool = False

# Per-compaction-run cache: (model, text_hash) -> int
# Cleared between compaction runs in ChatCompactionEngine.compact().
_token_cache: dict[tuple[str, str], int] = {}

# Fixed token cost used for image parts when litellm cannot count them.
_IMAGE_FIXED_TOKEN_COST = 1024


def _text_hash(text: str) -> str:
    return hashlib.md5(text.encode("utf-8", errors="replace"), usedforsecurity=False).hexdigest()


def estimate_tokens(text: str, model: str, *, use_chars4: bool = False) -> int:
    """Estimate tokens for a text string.

    Axis 10: uses litellm.token_counter by default; falls back to chars//4
    on error and emits ``token_counter_fallback`` once per process.

    Results are cached per (model, text-hash) within a compaction run.
    """
    global _token_counter_fallback_warned
    if use_chars4:
        return max(1, len(text or "") // 4)
    cache_key = (model, _text_hash(text or ""))
    if cache_key in _token_cache:
        return _token_cache[cache_key]
    try:
        m = model or "gpt-3.5-turbo"
        count = litellm.token_counter(model=m, text=text or "")
        if count and count > 0:
            _token_cache[cache_key] = count
            return count
    except Exception:
        pass
    # Fallback path.
    if not _token_counter_fallback_warned:
        _token_counter_fallback_warned = True
        logger.warning(
            "litellm.token_counter failed for model=%r; "
            "falling back to chars//4 for this process",
            model,
        )
    result = max(1, len(text or "") // 4)
    _token_cache[cache_key] = result
    return result


def estimate_tokens_for_turn(
    turn: dict,
    model: str,
    *,
    use_chars4: bool = False,
    events: "EventLog | None" = None,
) -> int:
    """Estimate tokens for a single turn dict.

    Axis 6: ``content`` may be ``str | list[dict]``.
    - str → estimate_tokens(content, model)
    - list[dict] → sum text parts + fixed cost per image part
    - Fallback: use ``text`` field if present, else empty string.
    """
    content = turn.get("content") if isinstance(turn, dict) else None
    if content is None:
        # Compactor input shape uses "text" field.
        text = turn.get("text", "") if isinstance(turn, dict) else str(getattr(turn, "text", ""))
        return estimate_tokens(text, model, use_chars4=use_chars4)

    if isinstance(content, str):
        return estimate_tokens(content, model, use_chars4=use_chars4)

    if isinstance(content, list):
        total = 0
        for part in content:
            if not isinstance(part, dict):
                continue
            part_type = part.get("type", "")
            if part_type == "text":
                total += estimate_tokens(part.get("text", ""), model, use_chars4=use_chars4)
            elif part_type in ("image_url", "image_path", "image"):
                # Fixed cost per image part (conservative estimate).
                total += _IMAGE_FIXED_TOKEN_COST
            else:
                # Unknown part type — estimate via JSON repr.
                total += estimate_tokens(json.dumps(part), model, use_chars4=use_chars4)
        return max(1, total)

    # Fallback: serialise to JSON and count.
    return estimate_tokens(json.dumps(content), model, use_chars4=use_chars4)


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
# Budget computation (Axis 1 + Axis 2 + derived assertions)
# ---------------------------------------------------------------------------


@dataclass
class ComputedBudgets:
    """Derived token budgets for a single compaction context.

    Computed once per engine init from CompactionConfig + model context.
    """
    main_pool: int          # T_max - T_SP  (main session's available tokens)
    head_budget: int        # tokens reserved for HEAD slice
    body_budget: int        # tokens reserved for BODY (summary)
    tail_budget: int        # tokens reserved for TAIL slice
    new_msg_budget: int     # tokens reserved for incoming user message
    B_M: int                # compactor LLM's own input budget
    main_M_room: int        # main session's middle room (after head+tail+new_msg)
    effective_trigger: int  # min(main_M_room, B_M) — used as the pre-frame trigger


def compute_budgets(
    cfg: "CompactionConfig",
    model: str,
    *,
    T_SP: int,
    T_comp_SP: int,
) -> ComputedBudgets:
    """Derive all token budgets from ratios + context window size.

    Parameters
    ----------
    cfg:
        CompactionConfig with ratio fields.
    model:
        LiteLLM model string (used to look up T_max via get_max_input_tokens).
    T_SP:
        Tokens consumed by the main session's system prompt.
    T_comp_SP:
        Tokens consumed by the compactor's own system prompt (Axis 2).
        Measured independently; does NOT come out of main_pool.
    """
    from reyn.llm.model_budget import get_max_input_tokens
    T_max = get_max_input_tokens(model)
    main_pool = T_max - T_SP
    head = int(cfg.head_ratio * main_pool)
    body = int(cfg.body_ratio * main_pool)
    tail = int(cfg.tail_ratio * main_pool)
    new_msg = int(cfg.new_msg_ratio * main_pool)
    B_M = T_max - T_comp_SP - body - cfg.section_caps_spec_tokens
    main_M_room = T_max - T_SP - head - tail - new_msg
    effective_trigger = min(main_M_room, B_M)
    return ComputedBudgets(
        main_pool=main_pool,
        head_budget=head,
        body_budget=body,
        tail_budget=tail,
        new_msg_budget=new_msg,
        B_M=B_M,
        main_M_room=main_M_room,
        effective_trigger=effective_trigger,
    )


def assert_static_bounds(cfg: "CompactionConfig", budgets: ComputedBudgets) -> None:
    """Assert invariants on the computed budgets.

    Called at ChatCompactionEngine.__init__ time so a misconfigured
    reyn.yaml fails fast at process start, not at first compaction.
    """
    ratio_sum = cfg.head_ratio + cfg.body_ratio + cfg.tail_ratio + cfg.new_msg_ratio
    assert ratio_sum <= 1.0, (
        f"CompactionConfig ratio sum = {ratio_sum:.4f} > 1.0 — "
        f"head_ratio + body_ratio + tail_ratio + new_msg_ratio must sum to ≤ 1.0"
    )
    assert budgets.B_M > 0, (
        f"B_M = {budgets.B_M} — compaction call self-bound violated "
        f"(try smaller body_ratio or larger model)"
    )
    assert budgets.effective_trigger > 0, (
        f"effective_trigger = {budgets.effective_trigger} — "
        f"model context too small for chosen ratios"
    )


# ---------------------------------------------------------------------------
# NewMsgExceedsBudgetError (Axis 11)
# ---------------------------------------------------------------------------


class NewMsgExceedsBudgetError(Exception):
    """Raised when the incoming user message exceeds new_msg_budget.

    This is a hard abort — the message is NEVER silently truncated.
    The caller should surface this to the user as a visible error.

    Attributes
    ----------
    new_msg_tokens:
        Estimated token count of the user's message.
    new_msg_budget:
        Budget available for the new message.
    """

    def __init__(self, new_msg_tokens: int, new_msg_budget: int) -> None:
        self.new_msg_tokens = new_msg_tokens
        self.new_msg_budget = new_msg_budget
        super().__init__(
            f"Incoming user message exceeds new_msg_budget: "
            f"{new_msg_tokens} tokens > {new_msg_budget} token budget. "
            f"The message cannot be processed without exceeding the model's "
            f"context window. Please reduce the message size."
        )


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
    max_tokens: int,
    model: str = "",
    *,
    use_chars4: bool = False,
    events: "EventLog | None" = None,
) -> list:
    """Return first turns until token budget exceeded — no turn count cap (Axis 3).

    A single turn that alone exceeds max_tokens is truncated (content kept,
    event emitted) and included in the result (Axis 7).
    """
    kept = []
    total = 0
    for t in turns:
        t_tokens = estimate_tokens_for_turn(t, model, use_chars4=use_chars4)
        if kept and total + t_tokens > max_tokens:
            # Would exceed budget — stop before adding this turn.
            break
        if t_tokens > max_tokens:
            # Single turn exceeds cap — include it, emit event (Axis 7).
            if events is not None:
                seq = t.get("seq", 0) if isinstance(t, dict) else getattr(t, "seq", 0)
                events.emit(
                    "turn_too_large_truncated",
                    turn_seq=seq,
                    original_tokens=t_tokens,
                    kept_tokens=max_tokens,
                    budget_kind="head",
                )
            kept.append(t)
            total += max_tokens
            break
        kept.append(t)
        total += t_tokens
    return kept


def trim_tail(
    turns: list,
    max_tokens: int,
    model: str = "",
    *,
    use_chars4: bool = False,
    events: "EventLog | None" = None,
) -> list:
    """Return last turns until token budget exceeded — no turn count cap (Axis 3).

    Walks from the tail backwards, then reverses the result.
    A single turn that alone exceeds max_tokens is included and event emitted
    (Axis 7).
    """
    kept: list = []
    total = 0
    for t in reversed(turns):
        t_tokens = estimate_tokens_for_turn(t, model, use_chars4=use_chars4)
        if kept and total + t_tokens > max_tokens:
            break
        if t_tokens > max_tokens:
            if events is not None:
                seq = t.get("seq", 0) if isinstance(t, dict) else getattr(t, "seq", 0)
                events.emit(
                    "turn_too_large_truncated",
                    turn_seq=seq,
                    original_tokens=t_tokens,
                    kept_tokens=max_tokens,
                    budget_kind="tail",
                )
            kept.append(t)
            total += max_tokens
            break
        kept.append(t)
        total += t_tokens
    return list(reversed(kept))


def hard_truncate_summary(
    summary_text: str,
    body_budget: int,
    model: str,
    events: "EventLog | None" = None,
    *,
    use_chars4: bool = False,
) -> str:
    """Post-process a compaction LLM body string to be ≤ body_budget tokens.

    Axis 9: deterministic hard truncation after the LLM returns.
    If summary_text is already within budget, returns unchanged.
    If over budget, truncates by character ratio (= tokens_kept / tokens_total
    * len) since detokenize is not guaranteed available.

    Emits ``body_summary_hard_truncated`` event when truncation occurs.
    """
    tokens = estimate_tokens(summary_text, model, use_chars4=use_chars4)
    if tokens <= body_budget:
        return summary_text
    # Char-truncate by ratio.
    ratio = body_budget / tokens
    keep_chars = max(1, int(len(summary_text) * ratio))
    truncated = summary_text[:keep_chars]
    if events is not None:
        events.emit(
            "body_summary_hard_truncated",
            original_tokens=tokens,
            kept_tokens=body_budget,
        )
    return truncated


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


class ChatCompactionEngine:
    """OS-internal compaction engine.

    Builds the LLM prompt from an input chunk, calls the model once via
    litellm directly, derives ``covers_through_seq`` deterministically, and
    returns a ``ChatSummary``.

    Axis 2: measures T_comp_SP at init time (independent of main session SP).
    Axis 8: exposes an ``asyncio.Lock`` (``compaction_lock``) that
        force_compact_now() callers must hold while compaction is in progress.
        History appends that need to be serialised with compaction must await
        this lock before appending.

    Parameters
    ----------
    model:
        LiteLLM model string (e.g. ``"gemini/gemini-2.5-flash-lite"``).
    events:
        Session-scoped EventLog for observability.
    cfg:
        CompactionConfig; used for use_chars4_estimate. When None a default
        config is used (for backward-compat test construction).
    T_SP:
        Tokens consumed by the main session's system prompt.
        Required for compute_budgets. Defaults to 0 (= no SP measured).
    """

    def __init__(
        self,
        model: str,
        events: "EventLog",
        cfg: "CompactionConfig | None" = None,
        *,
        T_SP: int = 0,
    ) -> None:
        self._model = model
        self._events = events
        # Axis 10: opt-out flag
        from reyn.config import CompactionConfig as _CC
        self._cfg: "CompactionConfig" = cfg if cfg is not None else _CC()
        self._use_chars4 = self._cfg.use_chars4_estimate

        # Axis 2: measure comp_SP token cost once at init.
        self._T_comp_SP: int = estimate_tokens(
            _COMPACTION_SYSTEM_PROMPT, model, use_chars4=self._use_chars4
        )

        # Axis 1 + derived: compute budgets and assert static bounds.
        self._budgets: ComputedBudgets = compute_budgets(
            self._cfg, model, T_SP=T_SP, T_comp_SP=self._T_comp_SP
        )
        assert_static_bounds(self._cfg, self._budgets)

        # Axis 8: compaction lock for synchronous force_compact path.
        self.compaction_lock: asyncio.Lock = asyncio.Lock()

    @property
    def budgets(self) -> ComputedBudgets:
        """Read-only access to the computed budget values."""
        return self._budgets

    async def compact(self, input_chunk: HistoryChunkToCompact) -> ChatSummary:
        """Run one compaction LLM call and return a ChatSummary.

        Axis 9: applies hard_truncate_summary to the returned topic_arc
        to ensure the body ≤ body_budget tokens deterministically.

        Raises on LLM error; callers wrap in try/except and emit
        ``compaction_failed`` if needed.
        """
        # Clear the per-run token cache for fresh estimates each compaction.
        _token_cache.clear()

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

        # Axis 9: hard-truncate the topic_arc to body_budget.
        topic_arc = hard_truncate_summary(
            str(parsed.get("topic_arc") or ""),
            self._budgets.body_budget,
            self._model,
            self._events,
            use_chars4=self._use_chars4,
        )

        return ChatSummary(
            topic_arc=topic_arc,
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
    "ComputedBudgets",
    "HistoryChunkToCompact",
    "NewMsgExceedsBudgetError",
    "assert_static_bounds",
    "compute_budgets",
    "compute_covers_through_seq",
    "estimate_tokens",
    "estimate_tokens_for_turn",
    "hard_truncate_summary",
    "trim_head",
    "trim_tail",
]
