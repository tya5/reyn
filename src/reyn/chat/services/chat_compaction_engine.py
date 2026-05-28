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
from typing import TYPE_CHECKING, Callable

import litellm

if TYPE_CHECKING:
    from reyn.config import (
        CompactionConfig,
        PhaseActResultsCompactionConfig,
        PlannerStepCompactionConfig,
    )
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
# ForceCompactRaceUnrecoveredError (ISSUE #6, lead-coder accept condition)
# ---------------------------------------------------------------------------


class ForceCompactRaceUnrecoveredError(Exception):
    """Raised when force_compact_now() exhausts max_passes still over budget.

    Option B race-recovery loop accepts up to N passes when concurrent
    sync history appends keep the prompt over the model's effective
    trigger. Past N, the contract is fail-fast: the caller must surface
    the unrecovered state rather than allow a silent over-budget LLM
    call. Pairs with `force_compact_race_unrecovered` event emit.

    Attributes
    ----------
    passes:
        Number of compaction passes attempted (= max_passes).
    """

    def __init__(self, passes: int) -> None:
        self.passes = passes
        super().__init__(
            f"force_compact_now exhausted max_passes={passes} still over budget. "
            f"Concurrent sync history appends are racing with synchronous "
            f"compaction. The prompt cannot be reduced below effective_trigger "
            f"within the race-recovery budget."
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
    Axis 4 (ISSUE #4): when ``system_prompt_provider`` is supplied, budgets
        are re-derived dynamically via :meth:`recompute_budgets` so that
        operator-editable SP changes (REYN.md reloads, skill catalog changes)
        are reflected before each pre-frame check.
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
        Static tokens consumed by the main session's system prompt.
        Ignored when ``system_prompt_provider`` is set (dynamic path).
        Defaults to 0 (= no SP measured).
    system_prompt_provider:
        Optional zero-argument callable that returns the current system
        prompt text.  When provided, :meth:`recompute_budgets` measures
        ``T_SP`` dynamically from the returned text so that operator-editable
        changes (REYN.md, skills catalog reloads) are reflected before each
        pre-frame check.  When ``None``, the static ``T_SP`` from ``__init__``
        is used for the lifetime of the engine.
    """

    def __init__(
        self,
        model: str,
        events: "EventLog",
        cfg: "CompactionConfig | None" = None,
        *,
        T_SP: int = 0,
        system_prompt_provider: Callable[[], str] | None = None,
    ) -> None:
        self._model = model
        self._events = events
        # Axis 10: opt-out flag
        from reyn.config import CompactionConfig as _CC
        self._cfg: "CompactionConfig" = cfg if cfg is not None else _CC()
        self._use_chars4 = self._cfg.use_chars4_estimate
        self._system_prompt_provider = system_prompt_provider

        # Axis 2: measure comp_SP token cost once at init.
        self._T_comp_SP: int = estimate_tokens(
            _COMPACTION_SYSTEM_PROMPT, model, use_chars4=self._use_chars4
        )

        if system_prompt_provider is not None:
            # Dynamic path (ISSUE #4): budgets computed via recompute_budgets()
            # which measures T_SP from the provider.  Defer assert_static_bounds
            # to the first recompute_budgets() call below.
            # Initialise with a placeholder so _budgets is always set.
            self._budgets: ComputedBudgets = compute_budgets(
                self._cfg, model, T_SP=T_SP, T_comp_SP=self._T_comp_SP
            )
            # Run the first recompute immediately so the provider is consulted
            # at init time and assert_static_bounds fires fail-fast.
            self.recompute_budgets()
        else:
            # Static path: T_SP is fixed for the session lifetime.
            self._budgets = compute_budgets(
                self._cfg, model, T_SP=T_SP, T_comp_SP=self._T_comp_SP
            )
            assert_static_bounds(self._cfg, self._budgets)

        # Axis 8: compaction lock for synchronous force_compact path.
        self.compaction_lock: asyncio.Lock = asyncio.Lock()

    def recompute_budgets(self) -> None:
        """Re-measure T_SP from the provider and recompute budgets.

        Called by session before each pre-frame check so dynamic SP state
        (= operator-editable REYN.md, skills catalog reloads) is reflected.

        When no ``system_prompt_provider`` was supplied at init, this method
        is a no-op — the static T_SP from ``__init__`` remains in effect.
        """
        if self._system_prompt_provider is None:
            return  # static T_SP from __init__ remains
        sp_text = self._system_prompt_provider()
        T_SP = estimate_tokens(sp_text, self._model, use_chars4=self._use_chars4)
        self._budgets = compute_budgets(
            self._cfg, self._model, T_SP=T_SP, T_comp_SP=self._T_comp_SP
        )
        assert_static_bounds(self._cfg, self._budgets)

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


# ---------------------------------------------------------------------------
# Step-results compaction (PR-N4)
# ---------------------------------------------------------------------------

# Stable key used to store the compacted summary in the step_results dict.
# Chosen to be visually distinct from step IDs (which are short hex-like
# strings) and to signal "this is a synthetic OS-inserted entry".
STEP_RESULTS_COMPACTED_KEY = "__compacted_step_summary__"

# System prompt for the step-results summariser.  Distinct from
# _COMPACTION_SYSTEM_PROMPT (chat axis) to avoid coupling the step-results
# concept to chat-history structure fields (topic_arc, decisions, etc.).
_STEP_RESULTS_SUMMARY_PROMPT = """\
You are summarising several prior plan-step outputs into a single concise summary.

Each input entry is a key-value pair where the key is the step ID and the value
is the step's output text.

Your task:
- Produce a single paragraph (or two at most) that preserves ALL actionable
  findings from the inputs: relevant code paths, function names, file paths,
  line numbers, key values, decisions made.
- Prioritise information that a later synthesis step would need to produce a
  correct final reply.
- Do NOT add commentary about what was summarised — output the summary text only.
Output ONLY the summary text. No headers, no bullet points, no JSON.
"""


async def compact_step_results(
    step_results: dict[str, str],
    *,
    engine: "ChatCompactionEngine",
    cfg: "PlannerStepCompactionConfig",
    events: "EventLog",
) -> dict[str, str]:
    """Return a new step_results dict where older entries are summarised.

    PR-N4 (FP-0008): step_results compaction.

    Algorithm
    ---------
    1. Estimate total token cost of all step_results values as plain text.
    2. Compute the effective threshold: ``cfg.summarize_older_threshold_tokens``
       when set, else ``step_results_ratio * engine.budgets.main_pool``.
    3. If total tokens ≤ threshold → return the input unchanged (identity).
    4. Split into *recent* (last ``cfg.recent_step_results_raw`` keys) and
       *older* (all keys before the recent window).
    5. Run one LLM summarisation call on the older values via the engine's
       model and proxy configuration.
    6. Apply ``hard_truncate_summary`` to bound the summary to ``body_budget``.
    7. Return ``{STEP_RESULTS_COMPACTED_KEY: summary, **recent_dict}``.
    8. Emit ``planner_step_results_compacted`` event.

    Bounded
    -------
    After compaction, token count of the returned dict's values is bounded by
    ``body_budget + sum(recent step tokens)`` where ``body_budget`` comes from
    the engine's ComputedBudgets (= same cap used for chat summary truncation,
    Axis 9).

    Failure modes
    -------------
    - If fewer than 2 step_results exist, or all entries fit in recent window,
      returns unchanged (= no-op).
    - If the summarisation LLM call fails, emits
      ``planner_step_results_compaction_failed`` and returns the input unchanged
      (= best-effort; does NOT raise — the step proceeds with the un-compacted
      prompt rather than crashing the plan run).

    Multimodal note: step_results values are plain strings (``dict[str, str]``),
    so no image-aware token counting is required.
    """
    if not step_results:
        return step_results

    keys = list(step_results.keys())
    use_chars4 = cfg.use_chars4_estimate
    model = engine._model  # noqa: SLF001 — internal use; ChatCompactionEngine owns this

    # Step 1: estimate total tokens of all step_results values.
    total_tokens = sum(
        estimate_tokens(v, model, use_chars4=use_chars4)
        for v in step_results.values()
    )

    # Step 2: effective threshold.
    if cfg.summarize_older_threshold_tokens is not None:
        threshold = cfg.summarize_older_threshold_tokens
    else:
        threshold = int(cfg.step_results_ratio * engine.budgets.main_pool)
        if threshold <= 0:
            # Model context info unavailable — skip compaction.
            return step_results

    # Step 3: identity check.
    if total_tokens <= threshold:
        return step_results

    # Step 4: split into older + recent.
    n_recent = max(0, cfg.recent_step_results_raw)
    recent_keys = keys[-n_recent:] if n_recent > 0 else []
    older_keys = keys[: len(keys) - n_recent] if n_recent > 0 else keys

    if not older_keys:
        # Nothing to compact (all entries are within the recent window).
        return step_results

    older_text = "\n\n".join(
        f"[Step {k}]\n{step_results[k]}" for k in older_keys
    )

    # Step 5 + 6: call LLM summariser, then hard-truncate.
    summary_text: str
    try:
        from reyn.llm.llm import proxy_kwargs
        extra = proxy_kwargs()
        effective_model = model
        if extra.get("custom_llm_provider") == "openai":
            parts = model.split("/", 1)
            if len(parts) == 2:
                effective_model = parts[1]

        response = await litellm.acompletion(
            model=effective_model,
            messages=[
                {"role": "system", "content": _STEP_RESULTS_SUMMARY_PROMPT},
                {"role": "user", "content": older_text},
            ],
            **extra,
        )
        raw_summary = (response.choices[0].message.content or "").strip()
        if not raw_summary:
            raise ValueError("step_results compaction LLM returned empty response")
        # Bound the summary to the engine's body_budget (Axis 9 pattern).
        summary_text = hard_truncate_summary(
            raw_summary,
            engine.budgets.body_budget,
            model,
            events,
            use_chars4=use_chars4,
        )
    except Exception as exc:  # noqa: BLE001 — best-effort; never raise
        logger.warning(
            "compact_step_results: LLM summarisation failed (%r); "
            "proceeding with un-compacted step_results",
            exc,
        )
        try:
            events.emit(
                "planner_step_results_compaction_failed",
                n_older=len(older_keys),
                error=repr(exc),
            )
        except Exception:  # noqa: BLE001
            pass
        return step_results

    # Step 7: build result dict.
    recent_dict = {k: step_results[k] for k in recent_keys}
    result: dict[str, str] = {STEP_RESULTS_COMPACTED_KEY: summary_text, **recent_dict}

    # Step 8: emit event.
    original_tokens = total_tokens
    summary_tokens = estimate_tokens(summary_text, model, use_chars4=use_chars4)
    recent_tokens = sum(
        estimate_tokens(step_results[k], model, use_chars4=use_chars4)
        for k in recent_keys
    )
    try:
        events.emit(
            "planner_step_results_compacted",
            n_older_compacted=len(older_keys),
            n_recent_kept=len(recent_keys),
            original_tokens=original_tokens,
            summary_tokens=summary_tokens,
            recent_tokens=recent_tokens,
        )
    except Exception:  # noqa: BLE001
        pass

    return result


# ---------------------------------------------------------------------------
# Phase act-loop control_ir_results compaction (PR-N5)
# ---------------------------------------------------------------------------

# Phase axis comp_SP — op-kind-aware structured preservation.
# Distinct from the chat axis comp_SP (_COMPACTION_SYSTEM_PROMPT) because
# phase op results carry specific data (paths, line numbers, exit codes) that
# the LLM must retain to continue acting on them; pure abstraction loses
# utility.
_PHASE_COMPACTION_SYSTEM_PROMPT = """\
You are summarising older `control_ir_results` from a phase's act loop
to keep the next prompt within the model's context budget.

For each older result, preserve op-kind-specific structured data:
  - grep:      keep matched paths + line numbers (e.g. "src/foo.py:42, src/bar.py:18")
  - file_read: keep path + byte size + line range (e.g. "src/foo.py L1-200, 8.3 KB")
  - shell:     keep cmd + exit code + last 5 lines of stdout (head/tail acceptable)
  - file_write / file_edit: keep path + byte delta + summary of change
  - web_fetch: keep url + http status + content-type
  - other:     keep kind + status + a short fact line

Do NOT generalise away path names, line numbers, exit codes, or http status
codes — the LLM uses these to plan its next op. Keep section budgets
tight; brevity matters more than narrative.
"""


async def compact_control_ir_results(
    older_results: list[dict],
    *,
    engine: "ChatCompactionEngine",
    cfg: "PhaseActResultsCompactionConfig",
    events: "EventLog",
    phase: str | None = None,
) -> list[dict]:
    """Return a list with ``older_results`` summarised into a single
    ``__compacted_phase_results__`` placeholder entry.

    PR-N5 (FP-0008): phase act-loop control_ir_results compaction.

    Algorithm
    ---------
    1. Estimate total token cost of ``older_results`` as plain JSON text.
    2. Compute the effective threshold: ``cfg.summarize_older_threshold_tokens``
       when set, else ``cfg.control_ir_results_ratio × engine.budgets.main_pool``.
    3. If total tokens ≤ threshold → return identity (older_results unchanged).
    4. Run one LLM summarisation call on older_results via ``_PHASE_COMPACTION_SYSTEM_PROMPT``
       and the engine's model + proxy configuration.
    5. Apply ``hard_truncate_summary`` against ``engine.budgets.body_budget``.
    6. Return a list of length 1 containing
       ``{"kind": "__compacted_phase_results__", "summary": <text>,
          "compacted_count": N, "original_tokens": T}``.
    7. Emit ``phase_act_results_compacted`` event.

    Bounded computation guarantee
    ------------------------------
    After compaction, token count of the returned list is bounded by
    ``body_budget`` (from hard_truncate_summary, same cap as chat summary
    truncation, Axis 9).

    Failure modes
    -------------
    - LLM error → emit ``phase_act_results_compaction_failed`` + return
      ``older_results`` unchanged (= identity, best-effort — same pattern as
      PR-N4 planner step axis, distinct from PR-N3 chat axis fail-fast).
      NEVER raises.

    Multimodal note
    ---------------
    ``control_ir_results`` items are op-result dicts like ``{"kind": "grep", ...}``,
    not multimodal Message turns.  Token estimation uses
    ``estimate_tokens(json.dumps(item), model)`` — NOT ``estimate_tokens_for_turn``
    which is for multimodal Message turns.
    """
    if not older_results:
        return older_results

    use_chars4 = cfg.use_chars4_estimate
    model = engine._model  # noqa: SLF001 — internal use; ChatCompactionEngine owns this

    # Step 1: estimate total tokens of older_results.
    total_tokens = sum(
        estimate_tokens(json.dumps(item, ensure_ascii=False), model, use_chars4=use_chars4)
        for item in older_results
    )

    # Step 2: effective threshold.
    if cfg.summarize_older_threshold_tokens is not None:
        threshold = cfg.summarize_older_threshold_tokens
    else:
        threshold = int(cfg.control_ir_results_ratio * engine.budgets.main_pool)
        if threshold <= 0:
            # Model context info unavailable — skip compaction.
            return older_results

    # Step 3: identity check.
    if total_tokens <= threshold:
        return older_results

    # Serialise older_results for the LLM call.
    older_text = json.dumps(older_results, ensure_ascii=False, indent=1)

    # Step 4 + 5: call LLM summariser, then hard-truncate.
    summary_text: str
    try:
        from reyn.llm.llm import proxy_kwargs
        extra = proxy_kwargs()
        effective_model = model
        if extra.get("custom_llm_provider") == "openai":
            parts = model.split("/", 1)
            if len(parts) == 2:
                effective_model = parts[1]

        response = await litellm.acompletion(
            model=effective_model,
            messages=[
                {"role": "system", "content": _PHASE_COMPACTION_SYSTEM_PROMPT},
                {"role": "user", "content": older_text},
            ],
            **extra,
        )
        raw_summary = (response.choices[0].message.content or "").strip()
        if not raw_summary:
            raise ValueError("phase act_results compaction LLM returned empty response")
        # Bound the summary to the engine's body_budget (Axis 9 pattern).
        summary_text = hard_truncate_summary(
            raw_summary,
            engine.budgets.body_budget,
            model,
            events,
            use_chars4=use_chars4,
        )
    except Exception as exc:  # noqa: BLE001 — best-effort; never raise
        logger.warning(
            "compact_control_ir_results: LLM summarisation failed (%r); "
            "proceeding with un-compacted control_ir_results",
            exc,
        )
        try:
            events.emit(
                "phase_act_results_compaction_failed",
                phase=phase,
                n_older=len(older_results),
                error=repr(exc),
            )
        except Exception:  # noqa: BLE001
            pass
        return older_results

    # Step 6: build result list.
    compacted_entry: dict = {
        "kind": "__compacted_phase_results__",
        "summary": summary_text,
        "compacted_count": len(older_results),
        "original_tokens": total_tokens,
    }

    # Step 7: emit event.
    summary_tokens = estimate_tokens(summary_text, model, use_chars4=use_chars4)
    try:
        events.emit(
            "phase_act_results_compacted",
            phase=phase,
            n_older_compacted=len(older_results),
            original_tokens=total_tokens,
            summary_tokens=summary_tokens,
        )
    except Exception:  # noqa: BLE001
        pass

    return [compacted_entry]


__all__ = [
    "ChatCompactionEngine",
    "ChatSummary",
    "ChatSummaryRaw",
    "ComputedBudgets",
    "HistoryChunkToCompact",
    "ForceCompactRaceUnrecoveredError",
    "NewMsgExceedsBudgetError",
    "STEP_RESULTS_COMPACTED_KEY",
    "assert_static_bounds",
    "compact_control_ir_results",
    "compact_step_results",
    "compute_budgets",
    "compute_covers_through_seq",
    "estimate_tokens",
    "estimate_tokens_for_turn",
    "hard_truncate_summary",
    "trim_head",
    "trim_tail",
]
