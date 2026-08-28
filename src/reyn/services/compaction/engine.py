"""CompactionEngine — OS-internal LLM-driven chat history compaction.

PR-N3 (FP-0008, 11-axis): a direct Python helper for chat compaction.
One LLM call is retained, with no phase-frame overhead — the compaction
prompt and postprocessing are inlined here.

PR-N6 (FP-0008): adds overflow retry loop + adaptive token estimation learner.
Budget allocation migrated from ratio fields to integer component_weights /
section_weights (sum-arbitrary, normalised at compute_budgets() time).

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
- ``compute_budgets`` / ``assert_static_bounds`` enforce the weight invariants
  at engine init time so a misconfigured reyn.yaml fails fast (Axis 3 derived).
- PR-N6: ``ContextOverflowError`` / ``CompactionOverflowError`` / ``UnrecoveredError``
  provide fail-fast semantics for the retry_loop (chat axis = fail-fast, unlike
  planner step axis / phase axis which are best-effort).
- PR-N6: ``retry_loop`` shrinks ``(head, tail)`` token count monotonically per
  iteration (``raw_middle`` is not monotonic on its own — see the function's
  own "Bounded termination proof" docstring) until the prompt fits or a
  structured ``UnrecoveredError`` is raised — a stops-with-a-defined-error
  guarantee, not a fits-eventually one; a transient failure (5xx, rate limit)
  can raise too, not only genuine unshrinkable overflow (#4947).

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
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from reyn.llm.json_parse import loads_lenient
from reyn.llm.litellm_bootstrap import (
    LitellmWarmingInBackgroundError,
    ensure_litellm_ready_or_defer,
)
from reyn.prompt import compaction as _prompt_compaction
from reyn.runtime.error_format import is_quota_exhausted_error

if TYPE_CHECKING:
    from reyn.config import CompactionConfig
    from reyn.core.events.events import EventLog
    from reyn.llm.model_resolver import ModelResolver
    from reyn.runtime.services.token_multiplier_learner import TokenMultiplierLearner

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Token-counter fallback tracking (Axis 10)
# ---------------------------------------------------------------------------

# #3671 P1: read-then-written with no synchronization, safe only because
# nothing but the main thread ever called into this module. A planned
# startup-warming thread (#3671 P2/P3, not added by this PR) would call
# estimate_tokens() concurrently with ordinary turn processing. Owner
# directive: prefer non-lock exclusion where one exists over adding a lock
# (adding locks has its own embug risk). Here: no exclusion is needed at
# all — the worst outcome of two threads racing this exact flag is the
# warn-once log firing twice, a cosmetic duplicate, never a wrong value or
# a crash (unlike the two items below, both of which get a real fix, not a
# lock).
_token_counter_fallback_warned: bool = False

# #4395: a FAILURE, not a fallback COUNT, is what needs "between every-time
# and never-again". litellm.token_counter's own except-clause below already
# treats "fails once" as possibly transient (retries next call) — correct
# for a one-off. It becomes the WORST behaviour under a PERSISTENT,
# environmental failure (SSL egress blocked, no proxy reachable): every
# distinct text hits an uncached miss and pays the full call+timeout again,
# and estimate_tokens() runs synchronously from turn processing, so that
# wait blocks the UI each time (owner-reported: "reyn の UI が固まる",
# consistent with — not proven caused by — this shape, #4395).
# A cooldown, not a permanent give-up: an environmental failure CAN clear
# (a proxy comes up, egress opens) and this must not need a restart to
# notice. `time.monotonic()` — a wall-clock jump (NTP step, sleep/resume)
# must never shorten OR lengthen the wait.
_TOKEN_COUNTER_COOLDOWN_SECONDS = 60.0
_token_counter_cooldown_until: float = 0.0

# Process-lifetime cache: (model, text_hash) -> int. Keyed by a hash, not the
# raw text, so an entry stays tiny regardless of the source message's length
# (a long tool-output turn costs the same few bytes as a short one).
#
# Bounded LRU (#2937 revision): a cached count for a given (model, text) is
# valid for the process's entire lifetime — text is immutable and the
# tokenizer/fallback choice (`use_chars4_estimate`) is a fixed per-session
# config, never toggled mid-session, so there is no STALENESS reason to ever
# evict an entry. The bound below exists ONLY to cap memory, not for
# correctness: `CompactionEngine.compact()` used to `_token_cache.clear()`
# unconditionally at its start, which (a) was doing double duty as this
# module's ONLY size bound (no other prune/maxsize existed anywhere in the
# codebase) and (b) forced a fully-synchronous, on-the-event-loop re-estimate
# of the WHOLE conversation history on every turn immediately following a
# compaction (`build_history()` re-checks "total tokens <= trigger" every
# turn) — measured at ~470x slower cold vs warm on a real ~3000-message
# history, freezing the inline CUI for real seconds on a long chat. Losing
# the clear() naively (unbounded dict) would trade that freeze for a slow
# memory leak proportional to total-distinct-turns-ever, worsening in the
# exact same "long session" scenario. `OrderedDict` eviction kept recently-
# touched turns warm via `move_to_end` on every hit (LRU, by recency).
#
# #3671 P1 — LRU -> FIFO (behaviour change, stated plainly, not hidden): a
# concurrent read's membership-check-then-``move_to_end``-then-``[]`` was a
# check-then-act TOCTOU — a racing ``_token_cache_put`` evicting the SAME key
# in between raised ``KeyError`` out of ``move_to_end``/``__getitem__``
# instead of a clean miss (witnessed: 16 threads x 500 iterations,
# maxsize=4, reliable KeyError unlocked). Owner directive: prefer a
# non-lock fix over adding a lock. The actual defect is "reading mutates
# the cache" (the recency bump) — remove that, and a read becomes ONE
# ``dict.get()`` call, atomic under the GIL by construction, so the TOCTOU
# has nothing left to race. Trade-off: eviction is now by INSERTION order
# (oldest-inserted evicted first), not by how recently a turn was
# re-estimated — a real behaviour change. This module's whole point is
# capping memory for a process-lifetime cache of IMMUTABLE (model, text)
# results (see the correctness note above) — FIFO still bounds memory
# correctly, it just doesn't preferentially keep "hot" entries the way LRU
# did; re-estimating an evicted-then-revisited turn is a cache miss (cheap:
# ``estimate_tokens`` recomputes it), not a correctness issue.
_TOKEN_CACHE_MAXSIZE = 8192
_token_cache: "OrderedDict[tuple[str, str], int]" = OrderedDict()


def _token_cache_get(cache_key: "tuple[str, str]") -> "int | None":
    """FIFO-cache read: a miss returns None. #3671 P1: no longer bumps
    recency (see ``_token_cache``'s own docstring) — a single ``dict.get()``
    call, so there is no separate check-then-act step left to race."""
    return _token_cache.get(cache_key)


def _token_cache_put(cache_key: "tuple[str, str]", value: int) -> None:
    """FIFO-cache write: insert/overwrite in place (an overwrite does NOT
    move an existing key — eviction order is insertion order, #3671 P1),
    then evict the oldest entry if the bound is exceeded. A plain dict has
    no eviction primitive cheaper than O(n) — OrderedDict.popitem(last=False)
    is O(1)."""
    _token_cache[cache_key] = value
    if len(_token_cache) > _TOKEN_CACHE_MAXSIZE:
        _token_cache.popitem(last=False)


def token_cache_size() -> int:
    """Public read of the token-estimate cache's current entry count — the
    sanctioned surface for tests/diagnostics (mirrors testing.md's
    snapshot()-style-read guidance; the OrderedDict itself stays private)."""
    return len(_token_cache)

# Fixed token cost used for image parts when litellm cannot count them.
_IMAGE_FIXED_TOKEN_COST = 1024


def _text_hash(text: str) -> str:
    return hashlib.md5(text.encode("utf-8", errors="replace"), usedforsecurity=False).hexdigest()


def estimate_tokens(text: str, model: str, *, use_chars4: bool = False) -> int:
    """Estimate tokens for a text string.

    Axis 10: uses litellm.token_counter by default; falls back to chars//4
    when litellm.token_counter itself raises (a genuine failure), and emits
    ``token_counter_fallback`` the first time that happens in this process.

    ``count == 0`` (e.g. estimating an empty string) is a valid litellm
    result, not a failure, and does NOT trigger the fallback path (#2961).

    Results are cached per (model, text-hash) for the process's lifetime,
    bounded by an LRU eviction policy (see ``_token_cache`` docstring above)
    — but that cache is keyed by TEXT, so it cannot protect a genuinely
    persistent failure: a new text string is a cache miss regardless.
    #4395: a failure enters a ``_TOKEN_COUNTER_COOLDOWN_SECONDS`` cooldown —
    every call inside it skips litellm.token_counter entirely and goes
    straight to chars//4, no wait paid. This is deliberately a cooldown, not
    a permanent give-up: the failure MAY be transient (a proxy comes back,
    egress opens), and estimate_tokens() has no restart hook to notice that
    on its own — the next call after the cooldown expires re-probes once.
    """
    global _token_counter_fallback_warned
    if use_chars4:
        return max(1, len(text or "") // 4)
    cache_key = (model, _text_hash(text or ""))
    cached = _token_cache_get(cache_key)
    if cached is not None:
        return cached
    global _token_counter_cooldown_until
    in_cooldown = time.monotonic() < _token_counter_cooldown_until
    if not in_cooldown:
        try:
            # #4395 PR-2: non-blocking chokepoint variant — per-turn token
            # sizing runs BEFORE the first completion, so this can be the
            # FIRST litellm import in the process, and this function already
            # has a safe, cheap fallback (chars//4, below) for "no answer
            # yet". `ensure_litellm_ready_or_defer()` returns immediately
            # (never imports litellm on THIS thread) if litellm isn't warm,
            # kicking off the one dedicated background thread instead — see
            # litellm_bootstrap.py's own PR-2 section comment. Also fixes a
            # residual instance of PR-1's own defect at this exact site: the
            # old code called `ensure_litellm_ready()` and then did its own
            # unconditional `import litellm` right after WITHOUT checking
            # the return value — on a failure this re-attempted (and
            # re-failed) litellm's own slow, unbounded import on every call
            # outside its cooldown window, the same double-attempt shape
            # PR-1 fixed everywhere else but missed here (this module wasn't
            # part of PR-1's diff).
            litellm = ensure_litellm_ready_or_defer()
            m = model or "gpt-3.5-turbo"
            count = litellm.token_counter(model=m, text=text or "")
            # #2961: litellm.token_counter returns 0 (not an exception) for
            # an empty string — that is the correct answer, not a failure.
            # Only a raised exception (the `except` below) is a genuine
            # tokenizer failure that should fall back to chars//4.
            if count is not None and count >= 0:
                _token_counter_cooldown_until = 0.0  # healthy — clear any cooldown
                _token_cache_put(cache_key, count)
                return count
        except Exception:
            _token_counter_cooldown_until = (
                time.monotonic() + _TOKEN_COUNTER_COOLDOWN_SECONDS
            )
    # Fallback path — reached when litellm.token_counter raised (or, in
    # principle, returned something other than a non-negative int), OR the
    # cooldown above skipped the attempt entirely.
    # #3671 P1: check-then-set on this shared global has no lock — a
    # concurrent caller could also observe False before this one sets it
    # True, firing the warn-once notice more than once. Left unguarded on
    # purpose (owner directive: no lock without a real correctness need):
    # the only consequence is a duplicate log line, never a wrong count or
    # a crash (contrast ``_token_cache`` above and ``ensure_litellm_ready``,
    # ``_get_httpx_exc_types`` in llm.py — those get real fixes). Same
    # reasoning covers ``_token_counter_cooldown_until`` above: the worst
    # race is one caller re-probing litellm.token_counter slightly early or
    # late, never a wrong count.
    if not _token_counter_fallback_warned:
        _token_counter_fallback_warned = True
        logger.warning(
            "litellm.token_counter failed for model=%r; using chars//4 for "
            "this call and skipping the next %.0fs of calls (persistent-"
            "failure cooldown, #4395) — a later call re-probes automatically, "
            "no restart needed if the underlying cause clears.",
            model,
            _TOKEN_COUNTER_COOLDOWN_SECONDS,
        )
    result = max(1, len(text or "") // 4)
    _token_cache_put(cache_key, result)
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


def estimate_tokens_for_any_turn(
    turn: Any,
    model: str,
    *,
    use_chars4: bool = False,
) -> int:
    """#2957 PR-A/PR-B: the general canonical accounting entrypoint for a
    turn in EITHER shape — a live ``ChatMessage`` OR an already-serialised
    litellm wire dict (``_serialise_turn``'s output). NOT part of
    ``estimate_tokens_for_turn`` (kept dict-only + byte-unchanged by design
    — its ``turn.get("content")`` shape mirrors the compactor's dict turns).

    Two independent gaps this adapter closes, both because
    ``estimate_tokens_for_turn`` only ever looks at a dict's ``"content"``
    key:

    - **ChatMessage input** (PR-A): ``isinstance(turn, dict)`` is False for a
      live ``ChatMessage``, so every such turn used to fall through to
      ``estimate_tokens_for_turn``'s ``content is None`` fallback branch —
      undercounting images (``_IMAGE_FIXED_TOKEN_COST`` never applied) and
      ignoring ``tool_calls`` entirely.
    - **wire-dict input with top-level tool_calls** (PR-B, discovered while
      unifying the elide/advisor accounting): ``_serialise_turn`` puts
      ``tool_calls`` in a SEPARATE top-level wire-dict key (matching the
      real litellm request shape — see that method), not inside
      ``"content"``. A wire dict is a dict, so PR-A's original
      ``isinstance(turn, dict): return estimate_tokens_for_turn(turn, ...)``
      passthrough silently re-dropped ``tool_calls`` for exactly the wire
      dicts PR-B's unification now measures directly — the same class of
      undercount PR-A fixed for ChatMessage input, reappearing one call
      shape later. Both branches below fold ``tool_calls`` into extra
      content parts identically (of an unrecognised ``type``, so the
      existing "unknown part type -> JSON-repr" branch in
      ``estimate_tokens_for_turn`` counts them) WITHOUT running
      ``_serialise_turn``'s path-ref -> base64 materialisation (doing that
      would defeat the point of the fixed image cost, which exists
      precisely so counting never has to touch image bytes).

    A dict WITHOUT a ``"content"`` key (the compactor-input shape —
    ``{"text": ..., "seq": ..., ...}``, ``estimate_tokens_for_turn``'s own
    "text" fallback branch) passes through UNCHANGED even if it happens to
    carry a ``"tool_calls"`` sibling key — folding would silently discard
    its ``"text"`` payload. Only a dict WITH a ``"content"`` key (a genuine
    wire dict) gets the fold.

    Used by every canonical-accounting call site: ``RouterHistoryBuffer.
    build_history`` / ``decompose_history_for_retry`` (wire dicts, post
    PR-B), ``ContextBudgetAdvisor._incremental_history_tokens`` (the same
    wire dicts, via ``build_history``), and ``trim_head``/``trim_tail`` (via
    ``_trim_groups`` below — dict turns from the router path, live
    ``ChatMessage`` turns from ``CompactionController._select_candidates``,
    which needs each turn's ``ChatMessage.seq`` to survive identity-based
    filtering against ``prev_cover`` and so cannot convert to wire dicts).
    NOT removable — it is the shared dispatcher every caller measuring
    "what does the provider actually see" goes through, regardless of which
    of the two live shapes (ChatMessage / wire dict) it holds.
    """
    if isinstance(turn, dict):
        # Only a genuine litellm WIRE dict (``_serialise_turn``'s output,
        # which always sets a "content" key — see that method) gets the
        # tool_calls fold below. A dict WITHOUT a "content" key is the
        # compactor-input shape (``{"text": ..., "seq": ..., ...}`` —
        # ``estimate_tokens_for_turn``'s own "text" fallback branch), which
        # may carry "tool_calls" as an unrelated sibling metadata field —
        # folding would silently discard its "text" payload. Passthrough
        # unchanged for that shape, exactly PR-A's original dict contract.
        if "content" not in turn:
            return estimate_tokens_for_turn(turn, model, use_chars4=use_chars4)
        content = turn.get("content")
        tool_calls = turn.get("tool_calls")
    else:
        content = getattr(turn, "content", None)
        tool_calls = getattr(turn, "tool_calls", None)

    if not tool_calls:
        if isinstance(turn, dict):
            return estimate_tokens_for_turn(turn, model, use_chars4=use_chars4)
        return estimate_tokens_for_turn({"content": content}, model, use_chars4=use_chars4)

    parts = list(content) if isinstance(content, list) else (
        [{"type": "text", "text": content}] if content else []
    )
    parts = parts + [
        {"type": "tool_call", "tool_call": tc} for tc in tool_calls
    ]
    return estimate_tokens_for_turn({"content": parts}, model, use_chars4=use_chars4)


# ---------------------------------------------------------------------------
# Dataclasses (replace the YAML artifact schemas)
# ---------------------------------------------------------------------------


@dataclass
class HistoryChunkToCompact:
    """Input to the compaction engine.

    #4389: ``section_token_caps`` is a HINT to the LLM only — it is
    serialised straight into the compaction prompt's user content (see
    ``CompactionEngine.compact``) so the model can use it as *guidance* on
    how much room each section has. It does NOT bound anything
    deterministically, and a caller passing a tighter value here has no
    effect on what actually gets trimmed. The value that IS enforced,
    deterministically, is ``CompactionEngine._budgets.body_budget`` —
    computed once at ``CompactionEngine.__init__`` from ``CompactionConfig``
    (see ``compute_budgets``/``ComputedBudgets.section_caps``) and applied to
    ``topic_arc`` via the T2 (LLM re-summarize) / T3 (``hard_truncate_summary``)
    passes in ``compact``, independent of whatever this field says on a given
    call. Confirmed live (real LLM): setting ``topic_arc`` here to ~40 tokens
    had zero effect on the actual trim.
    """
    new_turns: list[dict]                          # [{role, text, seq, ...}]
    section_token_caps: dict                       # {topic_arc, decisions, ...} — LLM hint only, see docstring above
    previous_summary: dict | None = None           # prior ChatSummary or None


@dataclass
class ChatSummaryRaw:
    """LLM output before deterministic seq derivation.

    #4951-B: ``new_turn_seqs`` (the LLM's echo of every input turn's
    ``seq``) is REMOVED from this dataclass — 0 consumers (measured):
    this class is never constructed anywhere in the tree (``compact()``
    builds ``ChatSummary`` directly from the parsed response dict, never
    through this type), and ``ChatSummary.to_dict()`` never included the
    field even before this removal (see the #4951-A comment at this
    module's ``compact()`` call site). Nothing downstream loses a value
    it was reading."""
    topic_arc: str
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
    # #4703 axis①: the compaction LLM call's OWN usage — not persisted (see
    # to_dict() below, which does NOT list these), read once by
    # CompactionController to enrich its compaction_completed event. Owner's
    # own complaint: the conversation-face marker already exists
    # ("[↑ N turns compacted]"), what's missing is that it never showed the
    # real money this call spent. None only if usage genuinely could not be
    # read off the response (never coerced to 0 — the same real-figure-vs-
    # unknown discipline #4691's gutter work already applies).
    prompt_tokens: "int | None" = None
    completion_tokens: "int | None" = None
    cost_usd: "float | None" = None

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

    PR-N6: adds ``section_caps`` dict derived from section_weights normalised
    to body_budget.  Used by the compaction controller to populate
    HistoryChunkToCompact.section_token_caps.
    """
    main_pool: int          # T_max - T_SP  (main session's available tokens)
    head_budget: int        # tokens reserved for HEAD slice
    body_budget: int        # tokens reserved for BODY (summary)
    tail_budget: int        # tokens reserved for TAIL slice
    new_msg_budget: int     # tokens reserved for incoming user message
    B_M: int                # compactor LLM's own input budget
    main_M_room: int        # main session's middle room (after head+tail+new_msg)
    effective_trigger: int  # min(main_M_room, B_M) — used as the pre-frame trigger
    section_caps: dict = field(default_factory=dict)  # PR-N6: per-section token caps


def compute_budgets(
    cfg: "CompactionConfig",
    model: str,
    *,
    T_SP: int,
    T_comp_SP: int,
    t_max_override: int | None = None,
) -> ComputedBudgets:
    """Derive all token budgets from component_weights + context window size.

    PR-N6: uses integer component_weights normalised by their sum.

    Parameters
    ----------
    cfg:
        CompactionConfig with component_weights / section_weights dicts.
    model:
        LiteLLM model string (used to look up T_max via get_max_input_tokens).
    T_SP:
        Tokens consumed by the main session's system prompt.
    T_comp_SP:
        Tokens consumed by the compactor's own system prompt (Axis 2).
        Measured independently; does NOT come out of main_pool.
    t_max_override:
        #4885: when set, used IN PLACE of ``get_max_input_tokens(model)`` —
        does not call it at all, so this never touches the real model's
        context window globally (owner's condition ③: "get_max_input_tokens
        を大域で下げない"). ``retry_loop``'s own binary-search-on-T_max
        recovery (for an HTTP 413 — a request-BODY-BYTE limit, which the
        real T_max says nothing about) is the ONLY caller that passes this;
        every other call site keeps deriving T_max from the model as before.
    """
    if t_max_override is not None:
        T_max = t_max_override
    else:
        from reyn.llm.model_budget import get_max_input_tokens
        T_max = get_max_input_tokens(model)
    main_pool = T_max - T_SP

    # PR-N6: normalise component_weights.
    cw = cfg.component_weights
    total_c = sum(cw.values())
    head = int((cw.get("head", 0) / total_c) * main_pool) if total_c > 0 else 0
    body = int((cw.get("body", 0) / total_c) * main_pool) if total_c > 0 else 0
    tail = int((cw.get("tail", 0) / total_c) * main_pool) if total_c > 0 else 0
    new_msg = int((cw.get("new_msg", 0) / total_c) * main_pool) if total_c > 0 else 0

    # PR-N6: derive per-section token caps from section_weights normalised to body_budget.
    sw = cfg.section_weights
    total_s = sum(sw.values())
    if total_s > 0 and body > 0:
        section_caps: dict = {
            name: int((w / total_s) * body) for name, w in sw.items()
        }
    else:
        # Fallback: use CompactionSectionCaps legacy values.
        sc = cfg.section_token_caps
        section_caps = {
            "topic_arc": sc.topic_arc,
            "decisions": sc.decisions,
            "pending": sc.pending,
            "session_user_facts": sc.session_user_facts,
            "artifacts_referenced": sc.artifacts_referenced,
        }

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
        section_caps=section_caps,
    )


class CompactionBudgetSelfConsistencyError(Exception):
    """Raised when a computed budget violates a required self-consistency
    invariant (``B_M > 0`` or ``effective_trigger > 0``).

    ``assert`` is deliberately NOT used for these two checks: CPython
    strips every ``assert`` statement under ``-O`` / ``PYTHONOPTIMIZE=1``,
    so an assert-based guard here would silently vanish in an optimized
    production run — the same failure class #2352 named ("assert is
    stripped by -O; raise instead so the guard cannot vanish"). If this
    invariant is violated, ``effective_trigger`` (or ``B_M``) would flow
    downstream as a non-positive number; in particular the elide decision
    ``total <= effective_trigger`` (``router_history_buffer.py``) is always
    FALSE against a non-positive ``effective_trigger`` (``total`` is a sum
    of non-negative token counts), so the elide/compact branch would be
    taken on every turn instead of failing fast here.

    Note: ``B_M <= 0`` has no standalone -O witness and cannot have one —
    ``effective_trigger = min(main_M_room, B_M)``, so ``B_M <= 0`` implies
    ``effective_trigger <= 0`` always (containment by construction via
    ``min``, not a witness gap; see the comment at the ``B_M`` raise site).

    Attributes
    ----------
    field:
        Which computed budget violated its invariant (``"B_M"`` or
        ``"effective_trigger"``).
    value:
        The computed (non-positive) value.
    """

    def __init__(self, field: str, value: int, detail: str) -> None:
        self.field = field
        self.value = value
        super().__init__(detail)


def assert_static_bounds(
    cfg: "CompactionConfig", budgets: ComputedBudgets, model: str
) -> None:
    """Validate invariants on the computed budgets.

    PR-N6: validates component_weights / section_weights (sum > 0, all >= 0).
    Called at CompactionEngine.__init__ time so a misconfigured
    reyn.yaml fails fast at process start, not at first compaction.

    #3027: the two self-consistency checks below (``B_M`` / ``effective_trigger``
    positivity) raise ``CompactionBudgetSelfConsistencyError`` instead of using
    ``assert``, because ``assert`` is removed entirely under ``python -O`` —
    see the class docstring.
    """
    # PR-N6 weight-based assertions (replaces the ratio_sum <= 1.0 check).
    cw = cfg.component_weights
    assert sum(cw.values()) > 0, (
        "CompactionConfig.component_weights sum = 0 — "
        "at least one component weight must be > 0"
    )
    assert all(w >= 0 for w in cw.values()), (
        f"CompactionConfig.component_weights has negative values: "
        f"{[k for k, v in cw.items() if v < 0]}"
    )
    sw = cfg.section_weights
    assert sum(sw.values()) > 0, (
        "CompactionConfig.section_weights sum = 0 — "
        "at least one section weight must be > 0"
    )
    assert all(w >= 0 for w in sw.values()), (
        f"CompactionConfig.section_weights has negative values: "
        f"{[k for k, v in sw.items() if v < 0]}"
    )
    if budgets.B_M <= 0:
        # #3027 co-vet: this branch has no standalone -O witness, and cannot
        # have one — effective_trigger = min(main_M_room, B_M), so
        # B_M <= 0 ⇒ effective_trigger = min(main_M_room, B_M) <= B_M <= 0
        # always. Any budget config that trips THIS branch necessarily also
        # trips the effective_trigger branch below, so the effective_trigger
        # -O witness (tests/services/test_3027_budget_guard_survives_optimize.py)
        # already exercises the same "raise survives -O" property for this
        # branch too. This is containment by construction (via `min`), not a
        # witness gap — do not add a same-shaped standalone B_M-only -O test.
        from reyn.llm.model_budget import get_max_input_tokens
        T_max = get_max_input_tokens(model)
        raise CompactionBudgetSelfConsistencyError(
            "B_M",
            budgets.B_M,
            f"B_M = {budgets.B_M} — compaction call self-bound violated. "
            f"model={model!r} context window T_max={T_max} tokens; "
            f"component_weights={dict(cw)}; body_budget={budgets.body_budget} "
            f"tokens. component_weights (especially the body/summary weight) "
            f"are too large for this model's context — reduce component_weights "
            f"or use a model with a larger context window."
        )
    if budgets.effective_trigger <= 0:
        from reyn.llm.model_budget import get_max_input_tokens
        T_max = get_max_input_tokens(model)
        raise CompactionBudgetSelfConsistencyError(
            "effective_trigger",
            budgets.effective_trigger,
            f"effective_trigger = {budgets.effective_trigger} — model context "
            f"too small for chosen component_weights. model={model!r} context "
            f"window T_max={T_max} tokens; component_weights={dict(cw)}; "
            f"main_M_room={budgets.main_M_room}, B_M={budgets.B_M} "
            f"(effective_trigger = min(main_M_room, B_M)). The system prompt "
            f"(and/or component_weights, especially the SP-adjacent weights) "
            f"is too large relative to this model's context — reduce the "
            f"system prompt / component_weights, or use a model with a "
            f"larger context window."
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
# PR-N6 exception classes (overflow + retry fail-fast)
# ---------------------------------------------------------------------------


class ContextOverflowError(Exception):
    """Server-side context limit detected on the main LLM call.

    Raised when the LLM API returns a BadRequestError / context-length
    exceeded error, or when the pre-call estimate exceeds T_max.  Triggers
    retry_loop to shrink head/tail/raw_middle and retry.

    Fail-fast on the chat axis: unlike the planner step axis and phase axis
    (which are best-effort and emit *_compaction_failed events instead of
    raising), the chat session MUST fit the context window or raise a visible
    error.  Silent over-budget calls degrade response quality in ways that are
    hard to diagnose.
    """


#: #3783 stage 1: the single shared "is this a context-overflow error"
#: predicate. Previously duplicated (and already-diverged) in 5 places —
#: router_loop.py's own ``_is_context_overflow_error``, 3 inline copies in
#: router_loop_driver.py, and a 4-keyword subset here that was MISSING
#: "too long"/"too large" (a real behaviour difference, not a cosmetic one:
#: an overflow message using either phrase alone was silently NOT recognised
#: at this one site — see the git history this constant replaces).
#:
#: Placed here (next to ``ContextOverflowError``, not in ``runtime``) per
#: the arc's own TODO (this module was already the intended home —
#: router_loop.py's old comment named it) and the architect's ruling: this
#: predicate answers "is this an overflow", which is a property of the
#: compaction/retry-loop domain the exception classes above already live
#: in — NOT "can shrinking recover from this" (a separate, broader
#: question #3783 stage 3 addresses; the two must not be merged into one
#: predicate — see ``is_context_overflow_error``'s own docstring).
_CONTEXT_OVERFLOW_KEYWORDS = (
    "context", "token", "length", "limit", "too long", "too large",
)


def is_context_overflow_error(exc: BaseException) -> bool:
    """True when *exc* looks like a provider context-length overflow.

    #3783 stage 1: the single owner for this question, replacing 5
    independent (and divergent) copies. Type-checked FIRST — litellm raises
    ``ContextWindowExceededError`` (a ``BadRequestError`` subclass) for a
    real provider-side overflow, a definitive positive — with a keyword
    match on the stringified exception as a fallback for everything else.

    The keyword fallback is NOT deleted (a litellm *proxy* can flatten a
    provider's typed error down to a bare ``BadRequestError`` or another
    generic exception, losing the specific type): `str(exc)` is a value the
    thing being classified writes freely, so it is fine as a fallback signal
    but must never be the ONLY signal when a stronger one (the type) is
    available. This predicate answers ONLY "is this overflow" — it says
    nothing about whether shrinking can fix it (that is
    ``services.compaction.engine``'s own recover-classification, #3783
    stage 3, a deliberately separate question living in this same module
    but not this function).

    #4381 stage 1: HTTP 413 (Request Entity Too Large — a request-BODY-byte
    limit, a different dimension entirely from the token-count limit
    ``ContextWindowExceededError`` represents) used to reach this predicate
    ONLY through the ``"too large"`` keyword fallback — the exact
    "stronger signal available, but not used" gap this function's own
    docstring already warns against. litellm/openai exceptions carry a
    real ``status_code`` attribute (``openai.APIStatusError.status_code``,
    set from the underlying ``httpx.Response`` — a status LiteLLM's proxy
    cannot flatten away the way it can flatten a typed exception class),
    checked here as the SAME kind of definitive, type-adjacent signal the
    ``ContextWindowExceededError`` isinstance check above already is.
    Classification for 413 is UNCHANGED by this (it already matched via
    the keyword) — recovery behaviour for it is #4381's later stages
    (deliberately untouched here); what changes is that the match no
    longer depends on the exception's message text containing "too large"
    at all, so a differently-worded 413 (a different provider/proxy, a
    non-English locale) is now caught too.

    #5329 (architect review): a provider usage-window/plan quota
    exhaustion (429 ``usage_limit_reached``) is NEVER a context overflow
    — but its free-text message ("The usage limit has been reached")
    matches this predicate's own ``"limit"`` keyword fallback, so it used
    to classify as True here at EVERY call site that reaches this
    function without its own quota guard first (#5256's outer
    ``_run_with_shrink`` gate always checks quota before calling this —
    unaffected either way — but ``_router_main_call``'s own except,
    router_loop_driver.py, calls this DIRECTLY with no such guard: a
    quota exhaustion striking THAT call site, after ``retry_loop``'s
    compact() call already succeeded once, would re-enter the shrink
    ladder there instead — the SAME wasteful class #5329's compact()-wrap
    fix closes at a DIFFERENT call site). Checked here, at the single
    shared predicate, rather than adding a guard at each of its call
    sites individually — #5329's own reason to exist is exactly a
    call-site-by-call-site guard missing one spot; a fix at the ONE
    predicate every site funnels through cannot have a missed spot.
    """
    if is_quota_exhausted_error(exc):
        return False
    # #4381 stage 1: checked BEFORE the litellm import below (and so
    # regardless of whether that import succeeds) — a plain attribute
    # read needs no litellm dependency at all, and this signal must not
    # be weaker than the fallback it is meant to replace.
    if getattr(exc, "status_code", None) == 413:
        return True
    try:
        # #4395 PR-2: non-blocking chokepoint variant — this classifier
        # already falls back to the keyword search below when litellm's own
        # exception class isn't available, so "litellm not warm yet" is a
        # safe case to defer rather than block on (see
        # litellm_bootstrap.py's own PR-2 section comment). In practice this
        # path runs only AFTER a real completion call already failed, so
        # litellm is almost always already warm by the time this runs — the
        # defer path exists for correctness, not because this specific site
        # is where the reported freeze occurred. Also fixes the same
        # residual PR-1-shaped double-attempt-on-failure defect
        # ``estimate_tokens`` above had (this module wasn't part of PR-1's
        # diff): the old code ignored ``ensure_litellm_ready()``'s return
        # value and did its own unconditional ``import litellm`` right after.
        litellm = ensure_litellm_ready_or_defer()
        if isinstance(exc, litellm.ContextWindowExceededError):
            return True
    except (ImportError, LitellmWarmingInBackgroundError):
        pass
    return any(kw in str(exc).lower() for kw in _CONTEXT_OVERFLOW_KEYWORDS)


class CompactionOverflowError(Exception):
    """The compaction LLM call itself exceeded its B_M budget.

    Raised when the compaction call (= the inner ``engine.compact()`` call
    inside retry_loop) returns a context-length error.  Triggers the same
    escalation path as ContextOverflowError: shrink raw_middle/tail/head and
    retry.
    """


class UnrecoveredError(Exception):
    """retry_loop exhausted all shrink paths; mathematical impossibility.

    Raised when head, tail, and raw_middle are all at their minimum budgets
    and the prompt still cannot fit.  This is the fail-fast terminal condition
    — the caller MUST surface this as a user-visible error rather than
    proceeding with an over-budget prompt.

    Attributes
    ----------
    reason:
        Human-readable description of the terminal condition.
    saw_byte_limit:
        #4954 (b), architect finding: whether an HTTP 413 (a
        request-BODY-BYTE limit) was observed during this call's shrink
        attempts — a real structured field, not something a caller
        should re-derive by string-matching ``reason`` (message wording
        is not a stable API; #4948/#4957 named the byte limit in prose
        for a HUMAN operator, not for a caller to parse).

        Deliberately named ``saw_byte_limit``, not ``is_byte_limit`` (or
        anything implying "this raise's own root cause"): at some raise
        sites (the same-cause cap, the generic "all shrink paths
        exhausted", max_iterations exhaustion) the branch taken does
        NOT itself determine the cause — the value here is the LAST
        recovered cause observed before this raise, which is correct
        for those sites but is a genuinely different claim from "this
        specific raise IS a byte-limit raise" (true only at the 3 sites
        where the branch itself is byte-limit-gated: the mid-split
        floor's byte-limit arm, the T_max binary-search floor, and the
        max_iterations byte-limit branch). A name like ``is_byte_limit``
        invites a future reader to assume the stronger claim at every
        site — the exact "same spelling, different meaning" trap this
        session hit repeatedly elsewhere tonight.

        "Last observed", not "sticky" (was a byte limit EVER seen this
        call, even if a later non-byte cause is what actually
        terminated it) — architect ruling: sufficient for the one
        measured symptom (owner's real machine always terminates ON the
        413, never past it to a different cause), and a "seen at all"
        version would require deliberately widening scope with no
        measured need for it yet. A caller that needs to react to a
        byte-limit exhaustion (#4954 (b): triggering a real compaction
        so the NEXT turn doesn't repeat the same overflow) reads this
        attribute.
    """

    def __init__(self, reason: str, *, saw_byte_limit: bool = False) -> None:
        self.reason = reason
        self.saw_byte_limit = saw_byte_limit
        super().__init__(reason)


# ---------------------------------------------------------------------------
# Deterministic helpers
# ---------------------------------------------------------------------------

# _COMPACTION_SYSTEM_PROMPT / _RESUMMARIZE_SYSTEM_PROMPT: relocated to
# reyn.prompt.compaction (SP prompt-package, Phase 2 §E) — module aliased
# above, re-bound to these underscore names so every call-site below (and the
# existing tests importing them from this module) is unchanged.
_COMPACTION_SYSTEM_PROMPT = _prompt_compaction.COMPACTION_SYSTEM_PROMPT
_RESUMMARIZE_SYSTEM_PROMPT = _prompt_compaction.RESUMMARIZE_SYSTEM_PROMPT


def compute_covers_through_seq(new_turn_seqs: list) -> int:
    """Return max(new_turn_seqs) or 0 when the list is empty.

    Deterministic; the LLM is not trusted to compute this correctly on
    weak models (a wrong value causes turn duplication or loss in
    Session.history).
    """
    if not new_turn_seqs:
        return 0
    return max(int(s) for s in new_turn_seqs)


# #4883: the wire shape a compaction response must have, for providers that
# support schema-constrained generation. Deliberately NOT routed through
# ``core.pipeline.schema``'s ``SchemaRegistry``/``to_json_schema`` (0062's
# path for a pipeline-authored, by-NAME-referenced schema) — this shape is
# engine-internal and fixed, never authored in YAML or looked up by name, so
# a plain JSON Schema dict is the more direct fit. All 5 keys are listed in
# "required" (OpenAI strict mode has no true-optional properties — "required"
# means the KEY must be present, not that an array/string can't be empty).
# Presence alone is weaker than this fix needs though: an empty topic_arc
# is schema-valid (an empty string is a valid value of its declared type) —
# :func:`_validate_chat_summary_fields` is the CONTENT-emptiness check
# schema constraints cannot express, and stays the floor for every
# provider, schema-constrained or not (see that function's own docstring).
#
# #4951-B: ``new_turn_seqs`` — the key that used to instruct the LLM to
# echo VERBATIM every input turn's ``seq`` — is REMOVED from this schema
# (and the system prompt, ``reyn.prompt.compaction``). #4951-A had already
# stopped READING the echo (``covers_through_seq`` is derived unconditionally
# from ``compact()``'s own input, never the LLM's output — see that call
# site's own #4951-A comment); this closes the other half — reyn no longer
# ASKS for it either. Owner ruling (#4951): the LLM path's information gain
# is zero by construction (the prompt forbade sorting/filtering/computing
# the max, so a model that ignored some turns still echoed every seq — the
# echo could only ever match reyn's own derivation or be wrong, never more
# correct), which is established by reading the prompt's own constraint,
# not by measurement — "測定は反例にしかならん" (a measurement can only
# ever supply a counterexample, never prove the omission harmless; if a
# real quality regression is observed post-landing, THAT is the
# counterexample and the basis for reverting, not its current absence).
_CHAT_SUMMARY_JSON_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "topic_arc": {"type": "string"},
        "decisions": {"type": "array", "items": {"type": "string"}},
        "pending": {"type": "array", "items": {"type": "string"}},
        "session_user_facts": {"type": "array", "items": {"type": "string"}},
        "artifacts_referenced": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "topic_arc", "decisions", "pending",
        "session_user_facts", "artifacts_referenced",
    ],
    "additionalProperties": False,
}


async def _supports_structured_output(model: str) -> bool:
    """#4883: whether ``model`` supports schema-constrained JSON generation
    (``litellm.supports_response_schema``) — the SAME capability precheck
    0062's ``AgentStep.schema`` path already uses and has in production
    (``router_loop.py``'s ``configure_structured_output`` precheck),
    including its provider-prefix strip (an operator-configured proxy alias
    like ``"openai/gemini-2.5-flash-lite"`` must be checked as the
    underlying gemini model, not misclassified as an unsupported literal
    OpenAI name — mirrors ``router_loop.py``'s own ``_precheck_model``
    derivation).

    Routed through :func:`ensure_litellm_ready` — the sole chokepoint
    equipped to handle a not-yet-warm or persistently-broken environment
    (cooldown, the background warming thread, log-routing setup) — never a
    second, independent ``import litellm`` (CI's
    ``test_4421_litellm_import_seam.py`` enforces this repo-wide; a prior
    revision of this function violated it). Unlike ``router_loop.py``'s own
    precheck (``ignore_cooldown=True`` — it has no fallback, a schema
    request was explicit), this call has a real fallback (plain
    ``json_object``), so it stays on the DEFAULT cooldown-protected path —
    a caller with a safe fallback is exactly what axis② cooldown exists to
    protect from repeatedly re-attempting a broken import.

    Never raises: any failure (litellm not importable/not ready yet, the
    capability query itself erroring) degrades to ``False`` — compaction is
    a context-window backstop (architect's #4883 framing), so an
    unavailable capability CHECK falls back to the existing
    ``json_object`` + post-parse-validation path, not a hard failure. This
    differs from 0062's own precheck (which raises
    ``StructuredOutputUnsupportedModelError`` on an explicit operator
    request for schema-constrained output) because compaction never had a
    schema-constrained contract to violate — it is opportunistically
    upgrading an existing best-effort call, not fulfilling an explicit
    per-turn request.
    """
    try:
        from reyn.llm.litellm_bootstrap import ensure_litellm_ready
        litellm = await asyncio.to_thread(ensure_litellm_ready)
    except Exception:  # noqa: BLE001 — capability probe, never the caller's failure
        return False
    if litellm is None or not hasattr(litellm, "supports_response_schema"):
        return False
    try:
        from reyn.llm.llm import proxy_kwargs
        extra = proxy_kwargs()
        precheck_model = (
            model.split("/", 1)[1] if extra.get("api_base") and "/" in model else model
        )
        return bool(litellm.supports_response_schema(precheck_model))
    except Exception:  # noqa: BLE001 — capability probe, never the caller's failure
        return False


def _validate_chat_summary_fields(parsed: dict) -> "list[str]":
    """#4883: the load-bearing field a compaction JSON response must
    actually carry — empty-string errors ([] = conforming), the same
    ``schema_validate_fn`` shape ``RouterLoop`` uses for 0062 (#2934).

    ``topic_arc`` IS the summary itself; missing/empty means the LLM
    produced a syntactically-valid but content-free response (e.g.
    ``"{}"``), which the old code accepted silently as success. The
    remaining 4 fields (``decisions`` / ``pending`` / ``session_user_facts``
    / ``artifacts_referenced``) are legitimately often empty (a short
    exchange may genuinely have none) — NOT validated here; only the field
    whose emptiness cannot be told apart from a dead response is.

    #4951-A: ``new_turn_seqs`` no longer gates validity here — the LLM's
    echo is no longer READ at all (``covers_through_seq`` is now derived
    unconditionally from ``compact()``'s own input, see the call site's
    #4951-A comment), so an empty/missing echo can no longer produce a
    wrong ``covers`` value the way it used to (the old fallback only
    caught an empty echo, never a non-empty-but-wrong one). Re-prompting
    the LLM over a field reyn never reads would spend a bounded re-prompt
    budget diagnosing a non-problem.
    """
    errors: "list[str]" = []
    if not str(parsed.get("topic_arc") or "").strip():
        errors.append("topic_arc is missing or empty")
    return errors


def _append_schema_reprompt(
    messages: "list[dict]", raw_response: str, errors: "list[str]",
) -> "list[dict]":
    """#4883: feed the invalid response + what was wrong with it back to the
    model for one bounded re-prompt attempt — the same "show the model its
    own mistake" shape 0062's re-prompt loop uses (router_loop.py), not a
    blind retry of the identical request."""
    return [
        *messages,
        {"role": "assistant", "content": raw_response},
        {
            "role": "user",
            "content": (
                "That response is invalid: "
                + "; ".join(errors)
                + ". Reply again with a complete JSON object covering all "
                "the new turns and a non-empty topic_arc."
            ),
        },
    ]


def _turn_role(t) -> "str | None":
    """The turn's wire role (``agent`` → ``assistant``), from a dict or a ChatMessage."""
    r = t.get("role") if isinstance(t, dict) else getattr(t, "role", None)
    return "assistant" if r == "agent" else r


def _is_assistant_with_tool_calls(t) -> bool:
    if _turn_role(t) != "assistant":
        return False
    tc = t.get("tool_calls") if isinstance(t, dict) else getattr(t, "tool_calls", None)
    return bool(tc)


def _group_tool_cycles(turns: list) -> "list[list]":
    """#2289: group each assistant-with-tool_calls turn together with its immediately-following
    ``role=tool`` result turns into ONE atomic trim unit (a "tool cycle"); every other turn is a
    singleton group. Trimming at group granularity keeps a tool_call and its results together, so a
    token boundary can never split the pair (which would reach the wire as a dangling call / orphan
    result → a provider 400 that the Layer-1 wire-repair then has to re-adjacency/synth/drop). This
    is the prevention layer: with it, that repair only fires on the genuine edges (an over-budget
    single cycle, or a rewind/interrupt mid-cycle)."""
    groups: list[list] = []
    i = 0
    n = len(turns)
    while i < n:
        t = turns[i]
        if _is_assistant_with_tool_calls(t):
            group = [t]
            j = i + 1
            while j < n and _turn_role(turns[j]) == "tool":
                group.append(turns[j])
                j += 1
            groups.append(group)
            i = j
        else:
            groups.append([t])
            i += 1
    return groups


def _emit_over_budget_group(events, group: list, budget: int, group_tokens: int, kind: str) -> None:
    """A single group alone exceeds ``budget`` and is KEPT WHOLE (never split → no result loss).

    A tool cycle emits ``tool_cycle_kept_whole_over_budget`` (NOT a truncation — the whole cycle
    survives, over budget). A non-cycle singleton keeps the existing Axis-7 ``turn_too_large_
    truncated`` (the single turn is included whole; content kept). NOTE (#1909): a tool cycle that
    exceeds the MODEL's HARD context limit (not merely this compaction budget) cannot be fixed by
    keep-whole — it would overflow at the provider. That is a tool-RESULT-size problem for op-level
    result truncation / context-narrowing (#1909), out of scope for this trim."""
    if events is None:
        return
    head = group[0]
    seq = head.get("seq", 0) if isinstance(head, dict) else getattr(head, "seq", 0)
    if _is_assistant_with_tool_calls(head):
        events.emit(
            "tool_cycle_kept_whole_over_budget",
            turn_seq=seq, group_tokens=group_tokens, budget=budget, budget_kind=kind,
        )
    else:
        events.emit(
            "turn_too_large_truncated",
            turn_seq=seq, original_tokens=group_tokens, kept_tokens=budget, budget_kind=kind,
        )


def _trim_groups(groups: "list[list]", max_tokens: int, model: str, use_chars4: bool,
                 events, kind: str) -> "list[list]":
    """Accumulate whole groups until the budget is exceeded — never splitting a group. A single
    group alone over budget is kept WHOLE (#2289). Returns the kept groups in ``groups`` order."""
    kept: list[list] = []
    total = 0
    for group in groups:
        # #2957 PR-A/PR-B: dispatches to estimate_tokens_for_turn for dict
        # turns (RouterHistoryBuffer's wire dicts, post PR-B) and adapts
        # live ChatMessage instances (CompactionController's candidate
        # selection, which still needs ChatMessage.seq downstream — see
        # estimate_tokens_for_any_turn's docstring for why that call site
        # cannot convert to wire dicts).
        g_tokens = sum(
            estimate_tokens_for_any_turn(t, model, use_chars4=use_chars4) for t in group
        )
        if kept and total + g_tokens > max_tokens:
            break  # adding this whole group would exceed budget — stop (never split it)
        if g_tokens > max_tokens:
            _emit_over_budget_group(events, group, max_tokens, g_tokens, kind)
            kept.append(group)
            break
        kept.append(group)
        total += g_tokens
    return kept


def trim_head(
    turns: list,
    max_tokens: int,
    model: str = "",
    *,
    use_chars4: bool = False,
    events: "EventLog | None" = None,
) -> list:
    """Return first turns until token budget exceeded — no turn count cap (Axis 3).

    #2289: trims at tool-cycle-GROUP granularity (``_group_tool_cycles``), so a boundary never
    splits a tool_call from its results. A single group alone exceeding ``max_tokens`` is kept whole
    (Axis 7 keep-whole — no split, no result loss). For a non-tool history every turn is a singleton
    group, so this is byte-identical to the message-level behavior.
    """
    kept = _trim_groups(
        _group_tool_cycles(turns), max_tokens, model, use_chars4, events, "head",
    )
    return [t for group in kept for t in group]


def trim_tail(
    turns: list,
    max_tokens: int,
    model: str = "",
    *,
    use_chars4: bool = False,
    events: "EventLog | None" = None,
) -> list:
    """Return last turns until token budget exceeded — no turn count cap (Axis 3).

    #2289: group-aware (see ``trim_head``) — accumulates whole tool-cycle groups from the tail, so
    a boundary never splits a pair. Byte-identical to message-level for non-tool histories.
    """
    groups = _group_tool_cycles(turns)
    kept_reversed = _trim_groups(
        list(reversed(groups)), max_tokens, model, use_chars4, events, "tail",
    )
    return [t for group in reversed(kept_reversed) for t in group]


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


class CompactionEngine:
    """OS-internal compaction engine.

    Builds the LLM prompt from an input chunk, calls the model once via
    litellm directly, derives ``covers_through_seq`` deterministically, and
    returns a ``ChatSummary``.

    Axis 2: measures T_comp_SP at init time (independent of main session SP).
    Axis 4 (ISSUE #4): when ``system_prompt_provider`` is supplied, budgets
        are re-derived dynamically via :meth:`recompute_budgets` so that
        operator-editable SP changes (REYN.md reloads, action catalog changes)
        are reflected before each pre-frame check.
    Parameters
    ----------
    model:
        Model CLASS name (``"standard"`` / ``"light"`` / ``"strong"``) OR a
        literal LiteLLM string.  It is resolved to a LiteLLM string via
        ``resolver`` at construction (#1172) — the engine NEVER hands an
        unresolved class to ``litellm.acompletion`` (which rejects it with
        ``BadRequestError model=standard``, failing every compaction trigger).
    events:
        Session-scoped EventLog for observability.
    resolver:
        Required ``ModelResolver`` used to resolve ``model`` to its LiteLLM
        string in ``__init__`` (same chain the router/main LLM call uses).
        By-construction guarantee (#1172): because resolution happens inside
        the engine, no construction site (chat / planner / phase) can leak an
        unresolved model class to litellm.  Pass ``ModelResolver({})`` for an
        already-resolved literal string (passthrough).
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
        changes (REYN.md, action catalog reloads) are reflected before each
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
        resolver: "ModelResolver | None" = None,
        recorder: object | None = None,
        recorder_agent: str | None = None,
    ) -> None:
        # #1172: resolve the model CLASS ("standard"/"light"/"strong") to its
        # LiteLLM string at construction — by-construction guarantee that no
        # downstream litellm.acompletion call (or estimate_tokens below) ever
        # receives an unresolved class (litellm rejects "standard" with
        # BadRequestError, failing every compaction trigger). A literal string
        # passes through unchanged. resolver defaults to an empty passthrough
        # ModelResolver so already-resolved callers/tests need not pass one;
        # every PRODUCTION construction site MUST pass its real resolver
        # (enforced by tests/llm/test_compaction_resolver_aware_1172.py so a future
        # caller cannot reintroduce the unresolved-class leak).
        if resolver is None:
            from reyn.llm.model_resolver import ModelResolver as _MR
            resolver = _MR({})
        self._model = resolver.resolve(model).model
        # #1190 stage (ii): BudgetTracker for cost recording (purpose=compaction)
        # via recorded_acompletion. None = unrecorded (e.g. ad-hoc/test engines).
        self._recorder = recorder
        # #1190 stage (iii) Part 4: agent for per-agent cost attribution. Chat
        # compaction = the session's agent_name; phase compaction = the run's
        # agent. None = attributed to no agent (legacy/test engines).
        self._recorder_agent = recorder_agent
        self._events = events
        # Axis 10: opt-out flag
        from reyn.config import CompactionConfig as _CC
        self._cfg: "CompactionConfig" = cfg if cfg is not None else _CC()
        self._use_chars4 = self._cfg.use_chars4_estimate
        self._system_prompt_provider = system_prompt_provider

        # Axis 2: measure comp_SP token cost once at init.
        # #1172 completion: use the RESOLVED self._model (not the raw class) so
        # token-counting + budget derivation see the real litellm model. The
        # static path (no system_prompt_provider) does NOT recompute_budgets(),
        # so __init__ is the only chance to resolve — passing the raw class
        # "standard" here made get_max_input_tokens() fall back to 128K and
        # handicapped every phase compaction (offload fired far too early).
        self._T_comp_SP: int = estimate_tokens(
            _COMPACTION_SYSTEM_PROMPT, self._model, use_chars4=self._use_chars4
        )

        if system_prompt_provider is not None:
            # Dynamic path (ISSUE #4): budgets computed via recompute_budgets()
            # which measures T_SP from the provider.  Defer assert_static_bounds
            # to the first recompute_budgets() call below.
            # Initialise with a placeholder so _budgets is always set.
            self._budgets: ComputedBudgets = compute_budgets(
                self._cfg, self._model, T_SP=T_SP, T_comp_SP=self._T_comp_SP
            )
            # Run the first recompute immediately so the provider is consulted
            # at init time and assert_static_bounds fires fail-fast.
            self.recompute_budgets()
        else:
            # Static path: T_SP is fixed for the session lifetime.
            self._budgets = compute_budgets(
                self._cfg, self._model, T_SP=T_SP, T_comp_SP=self._T_comp_SP
            )
            assert_static_bounds(self._cfg, self._budgets, self._model)

    def recompute_budgets(self) -> None:
        """Re-measure T_SP from the provider and recompute budgets.

        Called by session before each pre-frame check so dynamic SP state
        (= operator-editable REYN.md, action catalog reloads) is reflected.

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
        assert_static_bounds(self._cfg, self._budgets, self._model)

    @property
    def budgets(self) -> ComputedBudgets:
        """Read-only access to the computed budget values."""
        return self._budgets

    @property
    def model(self) -> str:
        """The RESOLVED LiteLLM model string (#1172) this engine budgets against.
        A public accessor so siblings (e.g. #1092 turn_budget) can derive their
        own budgets from the SAME resolved phase model rather than the cosmetic
        run-loop router_model."""
        return self._model

    async def _acompletion(
        self,
        messages: list[dict],
        *,
        response_format: dict | None = None,
        fallback_without_response_format: bool = False,
    ):
        """Single LLM call via the cost-observability chokepoint (#1190).

        Shared by ``compact`` (JSON response) and ``_resummarize_topic_arc``
        (text response). The chokepoint owns proxy_kwargs + provider-prefix
        strip + records usage (purpose="compaction") via the engine's recorder.

        ``fallback_without_response_format`` (#4883): passed through to
        ``recorded_acompletion`` — a provider that rejects ``response_format``
        outright gets ONE retry without it, same as the existing json-mode
        callers. Post-parse validation (:func:`_validate_chat_summary_fields`)
        is the floor either way, so this is defense-in-depth against the
        precheck below (:func:`_supports_structured_output`) being wrong for
        a specific provider, not the mechanism the fix depends on.
        """
        from reyn.llm.llm import recorded_acompletion
        return await recorded_acompletion(
            model=self._model,
            messages=messages,
            purpose="compaction",
            # #4206 T1 / #3785: compaction always follows Session.model
            # directly, never class_for_purpose — not subject to the
            # ②bounding model-class ceiling axis.
            model_class=None,
            recorder=self._recorder,
            agent=self._recorder_agent,
            response_format=response_format,
            fallback_without_response_format=fallback_without_response_format,
        )

    async def _resummarize_topic_arc(self, topic_arc: str, body_budget: int) -> str:
        """T2 (#271): LLM re-compression of an overshooting ``topic_arc``.

        Invokes the compactor model with the distinct relaxation prompt
        (``_RESUMMARIZE_SYSTEM_PROMPT``) to rewrite ``topic_arc`` to fit
        ``body_budget`` tokens — LLM-judgment loss (preserve decision-relevant)
        rather than the blind char-cut. Returns the original on any LLM error or
        empty response (T3 hard_truncate is the floor either way).
        """
        try:
            messages = [
                {"role": "system", "content": _RESUMMARIZE_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": (
                        f"Target budget: {body_budget} tokens.\n\n"
                        f"topic_arc to compress:\n{topic_arc}"
                    ),
                },
            ]
            response = await self._acompletion(messages)
            rewritten = (response.choices[0].message.content or "").strip()
            return rewritten or topic_arc
        except Exception as exc:  # noqa: BLE001 — re-summarize is best-effort; T3 floors it.
            self._events.emit("summary_resummarize_failed", error=str(exc))
            return topic_arc

    async def compact(self, input_chunk: HistoryChunkToCompact) -> ChatSummary:
        """Run one compaction LLM call and return a ChatSummary.

        Axis 9: applies hard_truncate_summary to the returned topic_arc
        to ensure the body ≤ body_budget tokens deterministically.

        Raises on LLM error; callers wrap in try/except and emit
        ``compaction_failed`` if needed.
        """
        # No `_token_cache.clear()` here (removed, #2937): text is immutable and
        # the tokenizer/fallback choice is a fixed per-session config, so a
        # cached (model, text) count is valid for the process's entire
        # lifetime — clearing it had no staleness justification. It forced a
        # COLD, synchronous, full-history re-tokenization on every turn right
        # after a compaction (build_history() re-estimates the whole raw
        # history every turn to check the elide trigger) — on a long
        # conversation this froze the event loop for real, user-visible
        # seconds, repeating every time compaction fired again as the chat
        # kept growing. The cache is now LRU-bounded (`_token_cache_put`)
        # instead of periodically nuked, so it stays warm (the perf fix)
        # without growing unboundedly (the memory-leak risk a naive removal
        # of the clear would have traded it for).

        user_content = json.dumps({
            "previous_summary": input_chunk.previous_summary,
            "new_turns": input_chunk.new_turns,
            "section_token_caps": input_chunk.section_token_caps,
        }, ensure_ascii=False)

        messages = [
            {"role": "system", "content": _COMPACTION_SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ]

        # #4883: the compaction JSON has no required fields (response_format
        # is {"type": "json_object"} — "must be JSON", not "must have these
        # keys"). A syntactically-valid-but-structurally-empty response
        # (e.g. "{}") previously passed through untouched: topic_arc
        # missing -> "" -> an empty summary silently overwrote the real
        # turns in history, with no error and no re-prompt. topic_arc IS
        # the summary, so its absence/emptiness is now a validation
        # failure, not a silently-accepted default — bounded re-prompt
        # (same shape 0062's schema_validate_fn uses in router_loop.py),
        # then raise if still invalid, joining the existing "raise on
        # empty response" safety net below (compaction_controller.py's
        # caller already wraps this in try/except and emits
        # compaction_failed + never calls _append_history on any raise here
        # — the ONLY change in observable behavior on failure is WHICH cases
        # raise, not what raising does).
        #
        # #4951-B: this used to also name new_turn_seqs as a second
        # load-bearing field validated here ("covers_through_seq decides
        # what's now unrecoverable via this call's own fallback"). No
        # longer true — new_turn_seqs is not in the schema at all now
        # (removed, see _CHAT_SUMMARY_JSON_SCHEMA's own comment), so there
        # is nothing to validate: covers_through_seq is derived
        # unconditionally from compact()'s own input below, never from
        # this response.
        max_attempts = 1 + max(0, int(getattr(self._cfg, "max_schema_reprompt_attempts", 1)))
        # #4883: schema-constrained generation when the model supports it
        # (json_schema, strict) — the GENERATION-time leg; json_object is
        # the fallback for models the capability precheck says do not (or
        # cannot be determined to) support it. Post-parse validation below
        # is the floor in BOTH cases (see _CHAT_SUMMARY_JSON_SCHEMA's own
        # docstring for why schema constraints alone can't replace it).
        #
        # DELIBERATELY the opposite of 0062's own unsupported-model
        # behavior — do not unify these two just because they share the
        # same capability check (architect/lead-coder, #4883): 0062 §2.1
        # raises StructuredOutputUnsupportedModelError with NO silent
        # fallback because an AgentStep's caller explicitly asked for
        # structured output — an unsupported model is THAT step's own
        # failure, contained to one step. Compaction never had a
        # schema-constrained contract to violate; it is an opportunistic
        # upgrade to an existing best-effort call, and its failure mode is
        # different in kind: raising here means the context window never
        # opens back up, i.e. the conversation itself cannot continue —
        # turning "summary quality isn't guaranteed" into "the session is
        # stuck" would be strictly worse than today. So compaction degrades
        # instead of raising on an unsupported model — the post-parse
        # validation floor is what still catches an empty/malformed
        # json_object response either way.
        #
        # The degrade decision is made HERE, before the call — not by
        # catching a provider rejection and retrying without
        # response_format (fallback_without_response_format stays False
        # below, on BOTH legs). 0062's own text is explicit about why (its
        # capability precheck exists precisely so the call never has to
        # find out the hard way): "Do NOT catch-classify a provider
        # rejection for this — a raw 400 can't be reliably told apart from
        # transient/other errors." A provider rejection despite a True
        # precheck (the capability table lagging reality) is therefore a
        # genuine call failure, not a signal to silently retry a
        # differently-shaped request — it propagates like any other
        # `_acompletion` failure, caught by CompactionController's
        # existing try/except (-> compaction_failed), which is the
        # pre-existing safety net this fix already leans on for the
        # exhausted-reprompt-budget case above.
        if await _supports_structured_output(self._model):
            _response_format = {
                "type": "json_schema",
                "json_schema": {
                    "name": "compaction_chat_summary",
                    "schema": _CHAT_SUMMARY_JSON_SCHEMA,
                    "strict": True,
                },
            }
        else:
            _response_format = {"type": "json_object"}
        _fallback_without_rf = False
        parsed: dict = {}
        raw = ""
        response = None
        reprompt_messages = list(messages)
        validation_errors: list[str] = []
        for attempt in range(max_attempts):
            response = await self._acompletion(
                reprompt_messages,
                response_format=_response_format,
                fallback_without_response_format=_fallback_without_rf,
            )
            raw = (response.choices[0].message.content or "").strip()
            if not raw:
                if attempt + 1 < max_attempts:
                    validation_errors = ["response was empty"]
                    self._events.emit(
                        "compaction_schema_invalid",
                        attempt=attempt,
                        max_attempts=max_attempts,
                        errors=validation_errors,
                    )
                    reprompt_messages = _append_schema_reprompt(
                        reprompt_messages, raw, validation_errors,
                    )
                    continue
                raise ValueError("compaction LLM returned empty response")

            parsed = loads_lenient(
                raw,
                on_raw_decode=lambda discarded_len, head: logger.warning(
                    "compaction_json_raw_decode_recovered: discarded %d bytes of "
                    "trailing garbage after valid JSON object. head=%r",
                    discarded_len,
                    head,
                ),
            )
            validation_errors = _validate_chat_summary_fields(parsed)
            if not validation_errors:
                break
            self._events.emit(
                "compaction_schema_invalid",
                attempt=attempt,
                max_attempts=max_attempts,
                errors=validation_errors,
            )
            if attempt + 1 < max_attempts:
                reprompt_messages = _append_schema_reprompt(
                    reprompt_messages, raw, validation_errors,
                )
                continue
            raise ValueError(
                f"compaction LLM response missing required fields after "
                f"{max_attempts} attempt(s): {validation_errors}"
            )

        # #4703 axis①: this call's own usage, for the compaction_completed
        # marker CompactionController emits below (never a resummarize-pass
        # call's usage — those are a rare, bounded-to-1-by-default backstop;
        # the primary compact() call is the dominant cost, and capturing
        # every resummarize pass too would need threading usage out of
        # _resummarize_topic_arc as well, a disclosed follow-up, not silent
        # scope creep). None if usage genuinely could not be read off the
        # response — never coerced to 0. Reflects the FINAL (successful)
        # attempt only — a re-prompt's own usage is not summed in, the same
        # "primary call dominates, re-prompt passes are a disclosed gap"
        # shape the resummarize-pass comment above already establishes.
        _usage = getattr(response, "usage", None)
        _prompt_tokens = getattr(_usage, "prompt_tokens", None)
        _completion_tokens = getattr(_usage, "completion_tokens", None)
        _cost_usd: "float | None" = None
        if isinstance(_prompt_tokens, int) and isinstance(_completion_tokens, int):
            from reyn.llm.pricing import TokenUsage, estimate_cost
            _cost_usd, _ = estimate_cost(
                self._model,
                TokenUsage(prompt_tokens=_prompt_tokens, completion_tokens=_completion_tokens),
            )

        # #4951-A: derive unconditionally from the INPUT reyn itself built
        # and passed to compact() — never from the LLM's output. This is
        # the fallback that used to fire only when the (now-removed) echo
        # was empty, promoted to the sole path (owner: "compaction は圧縮
        # 対象メッセージしか送らないはずなのでいらないと思うんだけど？" —
        # confirmed correct, #4951). The LLM's echo could only ever match
        # this value or be wrong (the prompt forbade sorting/filtering/
        # computing the max, so a model that ignored some turns still
        # echoed every seq) — there was no case where trusting the echo
        # over reyn's own input was more correct, only cases where it
        # silently wasn't (a non-empty-but-wrong echo used to pass through
        # unchecked; the old fallback only caught an EMPTY echo).
        #
        # #4951-B: the ``new_turn_seqs`` KEY itself is now REMOVED from
        # both the schema (``_CHAT_SUMMARY_JSON_SCHEMA``) and the system
        # prompt (``reyn.prompt.compaction``) — reyn no longer asks the LLM
        # to echo it at all (A had already stopped reading the echo; this
        # closes the other half). Owner ruling: the LLM path's information
        # gain is zero by construction (established by reading the
        # prompt's own constraint above, not by measurement — "測定は反例
        # にしかならん", a measurement can only ever supply a
        # counterexample). ``ChatSummaryRaw.new_turn_seqs`` is likewise
        # removed (0 consumers — this dataclass is never constructed
        # anywhere in the tree; ``ChatSummary.to_dict()`` never included
        # the field even before this removal).
        #
        # #4947 ③ note: this also supersedes ③'s own earlier clamp-based
        # fix for the same exposure (an over-claiming echo covering a
        # partial-slice remainder it was never offered) — with the echo
        # never read at all, there is nothing left to clamp; #4956/#4951-A
        # closes the exposure at its root instead of bounding its output.
        covers = compute_covers_through_seq(
            [t.get("seq", 0) for t in input_chunk.new_turns if isinstance(t, dict)]
        )

        # #271 — 3-tier topic_arc bounding (replaces the lone Axis-9 blind cut):
        #   T1 fit         — within budget → no LLM, unchanged (common case).
        #   T2 re-summarize — overshoot → LLM re-compression (judgment loss, the
        #                     user's "intentional summary compression is fine"),
        #                     bounded to ``resummarize_passes`` (default 1).
        #   T3 hard_truncate — deterministic floor, always applied last so
        #                     topic_arc ≤ body_budget is NEVER violated (the
        #                     dead-end-free bound; rare backstop after T2).
        body_budget = self._budgets.body_budget
        topic_arc = str(parsed.get("topic_arc") or "")
        passes = max(0, int(getattr(self._cfg, "resummarize_passes", 1)))
        for _ in range(passes):
            before_tokens = estimate_tokens(topic_arc, self._model, use_chars4=self._use_chars4)
            if before_tokens <= body_budget:
                break  # T1: fits
            topic_arc = await self._resummarize_topic_arc(topic_arc, body_budget)
            self._events.emit(
                "summary_resummarized",
                original_tokens=before_tokens,
                target_budget=body_budget,
                result_tokens=estimate_tokens(topic_arc, self._model, use_chars4=self._use_chars4),
            )
        topic_arc = hard_truncate_summary(  # T3: deterministic floor
            topic_arc,
            body_budget,
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
            prompt_tokens=_prompt_tokens,
            completion_tokens=_completion_tokens,
            cost_usd=_cost_usd,
        )


# ---------------------------------------------------------------------------
# PR-N6: retry_loop — bounded shrink loop for context overflow recovery
# ---------------------------------------------------------------------------


def _estimate_tokens_list(
    turns: list[dict],
    model: str,
    *,
    use_chars4: bool = False,
) -> int:
    """Estimate total tokens for a list of turn dicts."""
    return sum(
        estimate_tokens_for_turn(t, model, use_chars4=use_chars4)
        for t in turns
    )


# #4944 ①: the byte-axis counterpart of estimate_tokens_for_turn/
# _estimate_tokens_list above. A request-BODY-BYTE limit (an HTTP 413 from a
# proxy such as nginx, #4885/#4944) says nothing about TOKENS — the token
# estimators above cannot answer "how many bytes will this turn put on the
# wire", and building a second one is deliberately narrow in scope, not a
# competing accounting system: it measures the SAME boundary the token
# estimators already commit to (router_history_buffer.py's
# ``_serialise_turn`` docstring, #2957 PR-B — "this method's output is the
# CANONICAL quantity ... Do not reintroduce a second 'what does the
# provider see' quantity"). ``head``/``tail``/``new_msg`` passed into
# retry_loop are already ``_serialise_turn`` output (via
# ``decompose_history_for_retry``), so measuring THEIR wire-JSON byte size
# is measuring the same canonical quantity on the byte axis, not a second
# one.
def estimate_turn_bytes(turn: dict) -> int:
    """Estimate the wire-JSON byte size of one already-serialised turn dict.

    ``json.dumps(..., ensure_ascii=False).encode("utf-8")`` mirrors the
    shape litellm's own request serialisation takes (a UTF-8 JSON message
    list) closely enough to size the byte axis — it does not need to be
    litellm's EXACT byte-for-byte wire form (provider-specific wrapping is
    a separate, unmeasurable layer — see ``estimate_wire_bytes``'s own
    docstring for how that is handled, not chased here)."""
    return len(json.dumps(turn, ensure_ascii=False).encode("utf-8"))


def _estimate_bytes_list(turns: list[dict]) -> int:
    """Estimate total wire bytes for a list of already-serialised turn dicts."""
    return sum(estimate_turn_bytes(t) for t in turns)


@dataclass(frozen=True)
class WireByteBreakdown:
    """#5316: the 5-component byte breakdown ``estimate_wire_bytes`` computes
    but, before this, only ever summed and threw away — the diagnostic gap
    #5316 exists to close ("spill が効いたか/効かなかったか、出た後に判る手段
    が無い"). Each field is a byte COUNT only, never content (company
    environment — see the ``compaction_wire_bytes_measured`` emit sites'
    own comment)."""

    sp_bytes: int
    head_bytes: int
    summary_bytes: int
    tail_bytes: int
    new_msg_bytes: int

    @property
    def total(self) -> int:
        return (
            self.sp_bytes + self.head_bytes + self.summary_bytes
            + self.tail_bytes + self.new_msg_bytes
        )


def estimate_wire_bytes_breakdown(
    *,
    SP: str,
    head: list[dict],
    summary: dict | None,
    tail: list[dict],
    new_msg: dict,
) -> WireByteBreakdown:
    """Same 5 components ``estimate_wire_bytes`` sums, kept apart (#5316) so
    a caller can tell WHICH component dominated a given payload — the
    diagnostic ``estimate_wire_bytes``'s own single ``int`` return cannot
    answer. See that function's docstring for the shared KNOWN
    under-measurement disclosure (unchanged by this split)."""
    return WireByteBreakdown(
        sp_bytes=len(SP.encode("utf-8")),
        head_bytes=_estimate_bytes_list(head),
        summary_bytes=(
            len(json.dumps(summary, ensure_ascii=False).encode("utf-8"))
            if summary else 0
        ),
        tail_bytes=_estimate_bytes_list(tail),
        new_msg_bytes=estimate_turn_bytes(new_msg),
    )


def estimate_wire_bytes(
    *,
    SP: str,
    head: list[dict],
    summary: dict | None,
    tail: list[dict],
    new_msg: dict,
) -> int:
    """Estimate the total request-body byte size retry_loop's own payload
    would put on the wire — the byte-axis sibling of the token ``estimate``
    already computed in retry_loop's success path (same 5 components: SP,
    head, summary, tail, new_msg).

    KNOWN under-measurement (disclosed, not chased): this is ``history
    bytes + SP bytes`` — it does NOT include the tools-schema bytes (not
    threaded into retry_loop today) or whatever wrapper litellm/the
    provider add per-request. Architect's #4944① ruling: this under-count
    is deliberately absorbed by #4944②'s learned-from-413 ceiling rather
    than chased here with a provider-specific correction — the same
    "widen the floor, not the estimate" shape retry_loop's own token floor
    already uses (``SP + head_min + summary + tail_min + new_msg``, this
    function's byte-axis mirror).

    #5316: a thin wrapper over :func:`estimate_wire_bytes_breakdown` — kept
    as its own narrow ``int``-returning function since its two existing
    callers (``router_loop_driver.py``'s before/after spill comparison)
    only ever want the total, never the breakdown."""
    return estimate_wire_bytes_breakdown(
        SP=SP, head=head, summary=summary, tail=tail, new_msg=new_msg,
    ).total


# #3783 stage 2: same-cause consecutive-recover cap. Stage 3 will flip the
# default classification so more exception types recover-by-default instead
# of raising fatally; without this cap, a cause that shrinking can never fix
# (a bug misclassified as recoverable, not an actual overflow) would grind
# through all `max_iterations` LLM calls before giving up. This is a tighter,
# earlier check than `max_iterations` — it fires on the SAME cause repeating,
# not on iteration count alone, so a turn that alternates between two
# different real overflow causes is not penalised.
#
# #4947 ③ (architect-ruled, CI red on #4950): the counted quantity is
# consecutive same-cause recovers SINCE LAST PROGRESS, not since the start
# of the call — a ``compact()`` SUCCESS (including a partial one) resets
# both this counter and the recorded cause, at that one success site only
# (never on the OTHER escalation branches, which only MOVE content between
# head/tail/raw_middle rather than permanently reducing it — a cause
# recurring across a pure move is still evidence nothing is being fixed).
# Without the reset, a genuinely-shrinking cause (#3783 stage 3 arm (a)'s
# own motivating case) can trip this cap even while making real progress.
_MAX_CONSECUTIVE_SAME_CAUSE_RECOVERS = 2


def _learned_byte_limit_clause(
    *, last_accepted_wire_bytes: int | None, last_rejected_wire_bytes: int | None,
) -> str:
    """#5316: render the bracketed "this is what we learned about the
    gateway's real byte limit THIS turn" clause for a byte-limit terminal
    message — a read-back of the ``compaction_wire_bytes_measured``
    accepted=True (lower bound M)/accepted=False (upper bound N) pair
    already emitted this call, per architect's #5316 ruling ("新しい測定
    は要りません — 挟んだ値を終端 message に出す"). Degrades gracefully:
    a turn whose first attempt already 413s has no accepted bound to
    report yet (``last_accepted_wire_bytes is None``), and a turn that
    never got a chance to retry with a bigger payload has no rejected
    bound (should not happen on a byte-limit terminal path, but this
    function does not assume it). Byte counts only — never content."""
    if last_rejected_wire_bytes is None:
        return ""
    if last_accepted_wire_bytes is None:
        return (
            f" This gateway rejects at roughly {last_rejected_wire_bytes} "
            "bytes or fewer (no smaller size was accepted this turn)."
        )
    return (
        f" This gateway rejects at roughly {last_rejected_wire_bytes} bytes "
        f"(the last size that WAS accepted this turn was "
        f"{last_accepted_wire_bytes} bytes)."
    )


async def retry_loop(
    *,
    SP: str,
    head: list[dict],
    summary: dict | None,
    raw_middle: list[dict],
    tail: list[dict],
    new_msg: dict,
    cfg: "CompactionConfig",
    model: str,
    engine: "CompactionEngine",
    learner: "TokenMultiplierLearner",
    main_call: Callable[..., Awaitable[Any]],
    max_iterations: int = 8,
    spill_fn: "Callable[[dict], dict | None] | None" = None,
) -> Any:
    """Bounded shrink loop for context overflow recovery (PR-N6).

    On success (normal path or after shrink), calls ``learner.observe`` with
    the actual vs estimated token count so the adaptive estimator learns.

    Bounded termination proof
    -------------------------
    - The decreasing measure is ``(head, tail)`` token count, NOT
      ``raw_middle`` in isolation — ``raw_middle`` can GROW (Phase 1/2
      below move content FROM tail/head INTO it) and is not itself
      bounded below by ``head_min``/``tail_min``. #4947 ③ is what makes
      this measure well-founded: stage 1 (a failed ``compact()`` retrying
      a smaller slice of ``raw_middle``) touches ONLY ``raw_middle``,
      never ``head`` or ``tail`` — before ③, stage 1 moved the failing
      half of ``raw_middle`` INTO ``tail``, which could grow ``tail`` back
      up after Phase 1 had just shrunk it, defeating this proof (measured,
      #4947: a real 5-iteration period that reproduced the exact same
      ``(head, tail)`` state forever).
    - Lower bounds: ``head_min = budgets.head_budget``,
      ``tail_min = budgets.tail_budget`` (derived from
      ``component_weights["head|tail"] / total_weight * main_pool``).
    - Terminal condition: when ``head``/``tail`` are at or below their
      minimum token budgets AND ``raw_middle`` cannot be split any
      smaller (down to a single turn, #4947 ③'s floor) AND spilling that
      turn's content either was not available/possible or did not resolve
      the overflow (#5367③, below), ``UnrecoveredError`` is raised — this
      is a **structured-failure guarantee, not a success guarantee**: it
      promises retry_loop always STOPS instead of looping forever or
      silently dropping content, not that it always converges.
    - #5367③: ``spill_fn`` (optional, injected by the caller — this module
      never imports the caller, matching ``tool_result_cap.cap_tool_
      result_content``'s own ``save_fn``-injection style) is tried EXACTLY
      ONCE per distinct ``raw_middle[0]`` object, at BOTH terminal floors
      below (the byte-limit mid-split floor and the non-byte same-cause
      cap), regardless of which mode caused the overflow — a floor is a
      floor either way, and the fix (replacing an oversized tool-result
      body with a ref) is not byte/token-specific. A local per-call
      ``id()`` set bounds this to at most one extra ``continue`` per
      unique turn object, so it strictly tightens (never loosens) the
      existing ``max_iterations``-bounded termination proof above.
    - ``max_iterations=8`` is a safety cap independent of the above — even
      a well-founded decreasing measure can still take more steps than an
      operator wants to wait for real LLM calls, so this cap can fire
      first. Whether it is USUALLY the limiting factor is not measured
      here (#4947 found one specific repro where it was NOT the limiting
      factor — the mid-split floor raised first — but that is a single
      data point, not a frequency claim).
    - Scope: this proof covers ``retry_loop`` and its one current
      production call site (``router_loop_driver.py``'s reactive
      bounded-shrink call). ``retry_loop`` is also re-exported from
      ``reyn.services.compaction`` (``__init__.py``), so it is a public
      API surface even with a single caller today.
    - #3783 stage 2: a SAME-cause recover cap (``_MAX_CONSECUTIVE_SAME_CAUSE_
      RECOVERS``, currently 2) raises ``UnrecoveredError`` earlier than
      ``max_iterations`` when the identical exception type keeps recovering
      in a row — a real overflow shrinks its way to success within a few
      iterations, so a cause that keeps recurring unchanged is evidence
      shrinking cannot fix it (a misclassification, not an overflow), and
      grinding through the remaining iterations would just spend LLM calls
      to arrive at the same ``UnrecoveredError`` anyway. A per-iteration
      ``compaction_shrink_recovered`` audit-event names the cause, so this
      cap (and stage 3's later default-classification flip) is observable
      in the event log, not just inferred from the final exception.

    Failure-mode separation
    -----------------------
    - Chat axis (PR-N3 + PR-N6): fail-fast.
      ``ForceCompactRaceUnrecoveredError`` + ``UnrecoveredError`` both raise;
      the session MUST surface a user-visible error.
    - Planner step axis (PR-N4): best-effort — emits
      ``planner_step_results_compaction_failed`` and proceeds.
    - Phase axis (PR-N5): best-effort — emits

    Parameters
    ----------
    SP:
        Current system prompt text (used only for token estimation).
    head:
        HEAD turn list (oldest turns).
    summary:
        Current compacted summary dict or None.
    raw_middle:
        Middle turns not yet compacted.
    tail:
        TAIL turn list (most recent turns, verbatim).
    new_msg:
        Incoming user message turn dict.
    cfg:
        CompactionConfig (component_weights used for min budget derivation).
    model:
        LiteLLM model string.
    engine:
        CompactionEngine used for compaction calls.
    learner:
        TokenMultiplierLearner for adaptive estimation feedback.
    main_call:
        Async callable that performs the main LLM call.  Receives keyword
        args: SP, head, summary, tail, new_msg.  Should raise
        ``ContextOverflowError`` on context-length error.
    max_iterations:
        Safety cap (default 8).  Finite-by-construction termination means
        this cap is rarely reached.
    """
    from reyn.llm.model_budget import get_max_input_tokens
    from reyn.runtime.services.token_multiplier_learner import detect_content_type

    bg = engine.budgets
    head_min_tokens = bg.head_budget
    tail_min_tokens = bg.tail_budget
    use_chars4 = cfg.use_chars4_estimate

    _last_recover_cause: str | None = None
    _consecutive_same_cause = 0

    # #4885 (owner proposal, evaluated and approved by lead-coder): an HTTP
    # 413 is a request-BODY-BYTE limit — a different axis entirely from the
    # token budgets this whole ladder is built from (see this function's own
    # "Bounded termination proof" above: every shrink step and floor here is
    # measured in TOKENS). Lowering the EFFECTIVE T_max this invocation uses
    # is the only lever that makes the EXISTING token-shrink mechanics
    # respond to a byte-limit trigger at all — deliberately NOT a second,
    # byte-built ladder alongside this one (one resource, one gate; two
    # gates guarding the same resource is a shape this repo keeps re-
    # learning not to build). Binary search, not a fixed "shrink by half the
    # ceiling" guess: the byte/token ratio of whatever tripped the 413 is
    # unknown (a base64 attachment, a verbose non-English message, and a
    # repeated low-entropy block all have different ratios), so there is no
    # ratio to aim for — halving the SAME retry_loop-scoped T_max override
    # on each still-413 recovery converges in O(log T_max) steps regardless
    # of what the ratio turns out to be, the identical guarantee
    # ``max_iterations`` already relies on for the token-only case.
    #
    # Scope (owner condition ③): ``_t_max_override`` is a LOCAL variable —
    # passed to ``compute_budgets`` only, never to ``get_max_input_tokens``
    # or anywhere that would change the model's real context window for any
    # OTHER call. It dies with this ``retry_loop`` invocation; nothing
    # persists it past a single turn's shrink attempt (owner condition ②'s
    # "temporary" — this repo's OWN scoping mechanism already bounds the
    # lifetime to "this call", so there is no separate expiry to track).
    #
    # Floor (owner: "どこで諦めるか — そこはあなたが決めて"): ``SP`` and
    # ``new_msg`` are the two pieces of context this ladder NEVER shrinks
    # (``new_msg`` per this module's own #43 docstring: "NEVER dropped" —
    # dropping the user's own newest message would silently answer a
    # different question than the one asked; ``SP`` is the session's system
    # prompt, dropping it changes the agent's own instructions mid-turn).
    # Once a halved candidate T_max can no longer fit BOTH even with
    # head/tail/summary all at zero, no further halving can possibly
    # succeed — continuing would just re-hit the SAME terminal case one
    # halving later, burning ``max_iterations`` for no new information. That
    # is the floor: stop BEFORE trying a candidate that provably cannot fit
    # ``SP`` + ``new_msg`` alone, and raise the corrected diagnosis instead
    # (below) — not one more halved attempt, and not "exceeds T_max" (false
    # in this specific case: a byte limit was hit, not a token one).
    _last_recover_is_byte_limit = False
    # #5316: the learned ceiling — read back (not re-measured, per issue
    # #5316's own "新しい測定は要りません") into the terminal message below
    # when this loop ends on a byte limit. ``None`` until this loop's own
    # accepted=True/False pair has been observed at least once each; a
    # turn whose FIRST attempt already 413s carries no accepted bound yet
    # (the terminal message degrades gracefully — see its own comment).
    _last_accepted_wire_bytes: int | None = None
    _last_rejected_wire_bytes: int | None = None
    _t_max_override: int | None = None
    # #4947 ③ (architect-ruled): how many of ``raw_middle``'s turns the NEXT
    # ``compact()`` attempt should offer — ``None`` means "all of it" (the
    # normal, first-attempt case). Halved on each ``compact()`` failure,
    # reset to ``None`` on each success (a smaller *remainder* is then
    # attempted in full next time). This is the state that must actually
    # decrease for the split to terminate — see the shrink-escalation
    # comment below for why re-slicing the SAME ``raw_middle`` on every
    # iteration without persisting this would just recreate the old cycle.
    _compact_attempt_len: int | None = None
    # SP/new_msg never shrink (see the floor comment above) and never
    # change across iterations (both are fixed parameters) — computed once,
    # not on every floor check.
    _sp_tokens_floor = estimate_tokens(SP, model, use_chars4=use_chars4)
    _new_msg_tokens_floor = estimate_tokens_for_turn(new_msg, model, use_chars4=use_chars4)
    # #5367③: which raw_middle[0] objects a spill has already been tried
    # on this call — bounds the retry to at most one extra iteration per
    # unique turn (never the same object twice), so this can only tighten
    # the existing max_iterations-bounded termination proof, never loosen it.
    _spill_attempted_ids: "set[int]" = set()

    def _try_spill_first_mid_turn() -> bool:
        """#5367③: if a turn is available and spillable, spill it in place
        and report whether the caller should retry this iteration instead
        of raising. Never attempts the SAME object twice."""
        if spill_fn is None or not raw_middle:
            return False
        turn = raw_middle[0]
        if id(turn) in _spill_attempted_ids:
            return False
        _spill_attempted_ids.add(id(turn))
        spilled = spill_fn(turn)
        if spilled is None:
            return False
        raw_middle[0] = spilled
        return True

    for _iteration in range(max_iterations):
        # #4944①: tracks whether THIS iteration reached main_call — a
        # compact() call that overflows raises from a DIFFERENT payload
        # (raw_middle + section_caps, not head/summary/tail/new_msg) that
        # this function does not measure. Guards the failure-side
        # wire_bytes emission below so it never mislabels "nothing
        # resembling this was sent" as a rejected byte count.
        _this_iteration_called_main_call = False
        try:
            if raw_middle:
                # Compact raw_middle into the running summary.
                # Build section_token_caps from budgets.section_caps.
                section_caps = bg.section_caps if bg.section_caps else {
                    "topic_arc": 200, "decisions": 400, "pending": 400,
                    "session_user_facts": 200, "artifacts_referenced": 300,
                }
                # #4947 ③: offer only the first ``_compact_attempt_len``
                # turns when a prior attempt this call already failed —
                # ``None`` (no prior failure yet) offers all of it, the
                # same as before this change.
                _attempt_len = (
                    _compact_attempt_len if _compact_attempt_len is not None
                    else len(raw_middle)
                )
                input_chunk = HistoryChunkToCompact(
                    previous_summary=summary,
                    new_turns=raw_middle[:_attempt_len],
                    section_token_caps=section_caps,
                )
                try:
                    chat_summary = await engine.compact(input_chunk)
                    summary = chat_summary.to_dict()
                    # Only the ATTEMPTED slice is compacted — a smaller
                    # remainder (if any) stays in raw_middle for a later
                    # iteration. ``_compact_attempt_len`` is deliberately
                    # NOT reset to None (full remainder) here: a slice
                    # size discovered by halving down to what just worked
                    # is a reasonable size to try again on the remainder
                    # (measured: resetting to full every success made a
                    # uniformly-hard-to-compact input re-discover the same
                    # halving from scratch every round, taking ~11
                    # iterations for a fixture the OLD move-to-tail
                    # direction converged in 3 — #4950 review). Any slice
                    # that turns out too large for the new, smaller
                    # raw_middle just clips naturally at the slice above;
                    # no explicit clamping needed.
                    raw_middle = raw_middle[_attempt_len:]
                    # #4947 ③ (CI red on #4950, architect-ruled): reset the
                    # same-cause streak here, and ONLY here — not on any
                    # other escalation branch. The cap's own words below
                    # ("evidence shrinking cannot fix THAT cause" / "without
                    # shrinking ever changing anything") are already false
                    # the moment ONE slice compacts: this is the one branch
                    # where work is PERMANENTLY reduced (the compacted
                    # turns are gone from raw_middle for good, absorbed
                    # into ``summary``) — every OTHER escalation branch
                    # (Phase 1/2, the T_max-override halving) only MOVES
                    # content between head/tail/raw_middle, so the same-
                    # cause streak staying armed there is still correct: a
                    # cause recurring across a pure move is still evidence
                    # nothing is being fixed. A broader "reset on any
                    # progress" trigger would defeat the cap entirely
                    # (something changes on every escalation branch, every
                    # iteration). Both fields reset together — resetting
                    # only one leaves the other's next failure starting
                    # from "recover #2" instead of "#1".
                    _last_recover_cause = None
                    _consecutive_same_cause = 0
                    if raw_middle:
                        # #4947 ③: ``main_call`` never receives ``raw_middle``
                        # directly (only ``summary``/``head``/``tail``/
                        # ``new_msg``) — calling it now would silently drop
                        # this still-uncompacted remainder from what the
                        # LLM actually sees. Spend the rest of THIS
                        # iteration's budget compacting the remainder
                        # instead of calling main_call with an incomplete
                        # summary.
                        continue
                except Exception as exc:
                    # #5329: a provider usage-window/plan quota exhaustion
                    # (429 usage_limit_reached) is the ONE exception #3783
                    # stage 3's own "no per-exception-type allowlist"
                    # ruling below does not cover — it is genuinely never
                    # shrinkable (the window resets on a clock, not on
                    # input size, #5256's own finding for the SAME
                    # predicate at the outer _run_with_shrink gate). Real
                    # incident (owner, reyn-self): compact()'s own LLM call
                    # hit the SAME exhausted quota that had already failed
                    # main_call, got wrapped as CompactionOverflowError by
                    # the general rule below, and burned 2 more calls into
                    # the SAME dead window before the stage-2 cap finally
                    # gave up — 3 wasted round-trips during an active
                    # outage, not 1. Re-raised BARE (never wrapped), the
                    # SAME shape #5256's outer gate already uses and
                    # ``_handle_inbox_text``'s generic catch-all already
                    # handles safely — this is reuse of an existing,
                    # already-battle-tested seam, not a new one.
                    #
                    # Deliberately NARROWER than "remove RateLimitError
                    # from the shrinkable family" (architect ruling,
                    # #5329): an ordinary per-request 429 (no structured
                    # ``usage_limit_reached`` body) is NOT touched here —
                    # #3783 stage 3's own reasoning for including it stays
                    # intact, this predicate only carves out the ONE
                    # subtype that can never recover regardless of how
                    # many times shrinking retries it.
                    if is_quota_exhausted_error(exc):
                        raise
                    # #3783 stage 3 (owner-ratified): EVERY compact()-call
                    # exception now recovers by default — shrinking the
                    # input is a general remedy (a truncated JSON response,
                    # a transient 5xx, a rate limit), not an overflow-
                    # specific one, so gating the wrap on
                    # ``is_context_overflow_error`` locked out exactly the
                    # failures shrinking helps most (a response cut off by
                    # an output cap raises a plain ``JSONDecodeError`` that
                    # shares no keyword with the overflow predicate). No new
                    # predicate here — the discriminator that stops a
                    # never-recoverable cause from looping forever is the
                    # stage-2 cap below (``_MAX_CONSECUTIVE_SAME_CAUSE_RECOVERS``),
                    # not a per-exception-type allowlist at this site.
                    raise CompactionOverflowError(str(exc)) from exc

            _this_iteration_called_main_call = True
            response = await main_call(
                SP=SP,
                head=head,
                summary=summary,
                tail=tail,
                new_msg=new_msg,
            )

            # Success: observe actual vs estimated tokens for the learner.
            content_type = detect_content_type(new_msg.get("content"))
            sp_tokens = estimate_tokens(SP, model, use_chars4=use_chars4)
            head_tokens = _estimate_tokens_list(head, model, use_chars4=use_chars4)
            summary_tokens = estimate_tokens(
                json.dumps(summary, ensure_ascii=False) if summary else "",
                model, use_chars4=use_chars4,
            )
            tail_tokens = _estimate_tokens_list(tail, model, use_chars4=use_chars4)
            new_msg_tokens = estimate_tokens_for_turn(new_msg, model, use_chars4=use_chars4)
            estimate = sp_tokens + head_tokens + summary_tokens + tail_tokens + new_msg_tokens

            actual: int | None = None
            try:
                usage = getattr(response, "usage", None)
                if usage is not None:
                    actual = usage.prompt_tokens
            except Exception:
                pass

            if actual and estimate > 0:
                learner.observe(
                    model=model,
                    content_type=content_type,
                    estimate_tokens=estimate,
                    actual_tokens=actual,
                )

            # #4944①: measure-and-emit only (not consumed for any decision
            # until #5316's terminal-message read-back below) — see
            # estimate_wire_bytes's own docstring and this event's entry in
            # docs/reference/runtime/events.md for what "wire_bytes" does
            # and does not include. ``accepted=True``: this size was SENT
            # and SUCCEEDED, so the real limit is >= this value (a lower
            # bound). Paired with the ``accepted=False`` emission below
            # (lead-coder's TESTS-READ finding on this PR's first version:
            # a turn whose EVERY attempt 413s — owner's own real-machine
            # shape — would emit this event zero times without that
            # pairing, leaving no diagnostic trail at all for exactly the
            # case #4944 exists to diagnose).
            # #5316: the per-component breakdown (byte counts only — never
            # content, company environment) — the field this event's own
            # single ``wire_bytes`` total could never answer: WHICH of the
            # 5 components dominated. ``_last_accepted_wire_bytes`` feeds
            # the learned-limit read-back in the terminal message below.
            _accepted_breakdown = estimate_wire_bytes_breakdown(
                SP=SP, head=head, summary=summary, tail=tail, new_msg=new_msg,
            )
            _last_accepted_wire_bytes = _accepted_breakdown.total
            engine._events.emit(
                "compaction_wire_bytes_measured",
                wire_bytes=_accepted_breakdown.total,
                accepted=True,
                sp_bytes=_accepted_breakdown.sp_bytes,
                head_bytes=_accepted_breakdown.head_bytes,
                summary_bytes=_accepted_breakdown.summary_bytes,
                tail_bytes=_accepted_breakdown.tail_bytes,
                new_msg_bytes=_accepted_breakdown.new_msg_bytes,
            )

            return response

        except (CompactionOverflowError, ContextOverflowError) as _overflow_exc:
            # Compaction call or main call overflowed — fall through to
            # shrink. #3783 stage 2: name the cause + cap same-cause repeats.
            # #3783 stage 3: name the WRAPPED exception's type, not the
            # wrapper's — since stage 3, EVERY compact()-call failure is
            # wrapped as ``CompactionOverflowError`` (see its raise site
            # above), so ``type(_overflow_exc).__name__`` would always read
            # the same constant string regardless of what actually failed,
            # making two unrelated failures miscount as "the same cause
            # twice" against the cap below. ``__cause__`` is always set (both
            # wrap sites above raise ``... from exc``); the fallback to the
            # wrapper's own type name only matters for a hypothetical future
            # raise site that omits the chain.
            _cause = (
                type(_overflow_exc.__cause__).__name__
                if _overflow_exc.__cause__ is not None
                else type(_overflow_exc).__name__
            )
            if _cause == _last_recover_cause:
                _consecutive_same_cause += 1
            else:
                _last_recover_cause = _cause
                _consecutive_same_cause = 1
            # #4885: same status_code check `is_context_overflow_error` uses
            # (a real attribute litellm/openai set from the underlying HTTP
            # response, not string-matched) — checked on the ROOT cause, the
            # same one `_cause` above already names, so "413" and the cause
            # name agree about what actually happened.
            _last_recover_is_byte_limit = (
                getattr(_overflow_exc.__cause__, "status_code", None) == 413
            )
            if _last_recover_is_byte_limit and _this_iteration_called_main_call:
                # #4944①: the size that WAS SENT and got REJECTED — the
                # real limit is < this value (an upper bound), the pair to
                # the ``accepted=True`` emission above. ``head``/``tail``/
                # ``summary``/``new_msg`` still hold exactly what this
                # failed attempt sent — the shrink-escalation ladder below
                # has not run yet this iteration. Guarded on
                # ``_this_iteration_called_main_call``: a compact()-origin
                # 413 raised from a DIFFERENT, unmeasured payload (see the
                # flag's own comment above the loop).
                # #5316: same breakdown as the accepted=True site; feeds
                # the learned-limit read-back in the terminal message below.
                _rejected_breakdown = estimate_wire_bytes_breakdown(
                    SP=SP, head=head, summary=summary, tail=tail, new_msg=new_msg,
                )
                _last_rejected_wire_bytes = _rejected_breakdown.total
                engine._events.emit(
                    "compaction_wire_bytes_measured",
                    wire_bytes=_rejected_breakdown.total,
                    accepted=False,
                    sp_bytes=_rejected_breakdown.sp_bytes,
                    head_bytes=_rejected_breakdown.head_bytes,
                    summary_bytes=_rejected_breakdown.summary_bytes,
                    tail_bytes=_rejected_breakdown.tail_bytes,
                    new_msg_bytes=_rejected_breakdown.new_msg_bytes,
                )
            engine._events.emit(
                "compaction_shrink_recovered",
                cause=_cause,
                iteration=_iteration,
                consecutive=_consecutive_same_cause,
                t_max_override=_t_max_override,
            )
            # #4885: this cap is skipped for a byte-limit cause. It exists to
            # catch a TOKEN-shrink that keeps recovering the SAME cause
            # without ever changing anything — evidence shrinking cannot fix
            # THAT cause.
            #
            # #4947 ③: the ORIGINAL reasoning here said "the same cause
            # recovering repeatedly is the expected shape of active
            # binary-search progress" — that is false for a compact()-
            # origin 413 (measured, #4947: the search never even starts,
            # ``_t_max_override`` stays ``None`` the entire time, and the
            # SAME cause recovers because mid-splitting hadn't been fixed
            # yet, not because a search was in progress). The exemption
            # itself is left in place (①: whether it should key on
            # binary-search progress instead of the raw cause is a
            # separate, still-open question) — only the reasoning changes:
            # this cause's terminal case is NOT this cap, it is the floor
            # below (either the T_max-override floor for a main_call-origin
            # 413, or the mid=1-turn floor for a compact()-origin one) —
            # applying this cap on top of either floor would cut the
            # search off after only 2 recovers regardless of how much
            # headroom remains.
            if (
                _consecutive_same_cause > _MAX_CONSECUTIVE_SAME_CAUSE_RECOVERS
                and not _last_recover_is_byte_limit
            ):
                # #4954 (b): ``saw_byte_limit`` defaults to False here, and
                # this site is REACHABLE — its own guard clause
                # (``and not _last_recover_is_byte_limit``) is what makes
                # False the only value this raise can ever carry, not an
                # unreachable branch where the default happens not to
                # matter.
                # #5367③: before giving up, try spilling raw_middle[0]'s
                # content (mode-independent — a floor is a floor whether
                # the recurring cause is byte- or token-shaped) and retry
                # this iteration if it made progress.
                if _try_spill_first_mid_turn():
                    continue
                raise UnrecoveredError(
                    f"retry_loop: cause {_cause!r} recovered "
                    f"{_consecutive_same_cause} consecutive times (limit "
                    f"{_MAX_CONSECUTIVE_SAME_CAUSE_RECOVERS}) — the "
                    "turn-count shrink ladder attempted, plus any "
                    "content-level spill of raw_middle[0] this call had "
                    "available, did not resolve this cause; stopping "
                    "rather than exhausting max_iterations."
                ) from _overflow_exc

        # Shrink escalation: reduce context size monotonically.
        if raw_middle:
            # #4947 ③ (architect-ruled, replaces the old "move half of
            # raw_middle into tail" direction): ``compact()`` just failed
            # on the ``_attempt_len``-turn slice offered above. The OLD
            # direction pushed the failing half INTO ``tail`` — fattening
            # exactly the request ``main_call`` was about to retry, for a
            # failure that happened before compaction ever succeeded once.
            # This was a real bug, not a style choice: with ``tail`` never
            # shrinking back down (main_call's own overflow, if it also
            # 413s, refills raw_middle FROM tail via the Phase-1 branch
            # below, undoing this iteration's compaction attempt entirely)
            # the state returns to exactly where it started — this
            # function's own "Bounded termination proof" docstring
            # promises raw_middle/tail/head shrink monotonically, and this
            # line was the one violation (measured: #4947, a real
            # 5-iteration period with an always-413 ``compact()``).
            #
            # New direction: halve how much of raw_middle NEXT attempt
            # offers, leaving ``tail`` untouched — ``_compact_attempt_len``
            # is the state that must persist and decrease for this to
            # terminate (see its declaration above); recomputing a slice
            # from the unchanged ``raw_middle`` on every iteration without
            # persisting it would just recreate the same cycle.
            _current_attempt = (
                _compact_attempt_len if _compact_attempt_len is not None
                else len(raw_middle)
            )
            if _current_attempt <= 1:
                # Floor: even a single turn offered alone still fails —
                # halving further cannot produce a smaller nonzero slice.
                #
                # #4947 ③ (architect review on #4950): raising here is
                # correct ONLY for a byte limit. #3783 stage 3's own
                # reasoning ("EVERY compact()-call exception now recovers
                # by default — shrinking the input is a general remedy")
                # still applies to a NON-byte-limit failure (a transient
                # 5xx, a rate limit, a truncated JSON response) — this
                # split direction IS that shrinking; there is nothing
                # inconsistent about falling back to the OLD defer-to-tail
                # behavior for a single turn that keeps failing for a
                # reason unrelated to size. Compaction success is not a
                # precondition for ``main_call`` — deferring this one
                # turn and letting ``main_call`` run is a legitimate
                # outcome. This does NOT reopen #4947's own cycle: that
                # cycle was only reachable because a byte-limit cause is
                # EXEMPT from the same-cause cap above — a non-byte-limit
                # cause is NOT exempt, so ``_MAX_CONSECUTIVE_SAME_CAUSE_
                # RECOVERS`` already catches a genuinely stuck non-byte
                # cause on its own, with an accurate message, independent
                # of this floor (predates this arc).
                if _last_recover_is_byte_limit:
                    # #5367②: the OLD text past this point claimed
                    # "shrinking it further is not possible" — false. Only
                    # the TURN-COUNT floor is reached here (mid is already
                    # one turn; halving cannot produce a smaller nonzero
                    # slice, #4947 ③). #5367③: mid's CONTENT could still
                    # shrink — a turn whose body is a spillable tool result
                    # (owner: "spill は turn の中身を小さくします — 分割で
                    # はなく縮小") can be reduced without splitting it into
                    # more turns, so that is tried here (below) before
                    # this raise, not skipped.
                    if _try_spill_first_mid_turn():
                        continue
                    raise UnrecoveredError(
                        "retry_loop: HTTP 413 (a request-BODY-BYTE limit) "
                        "recurred compacting a single raw_middle turn "
                        "alone — mid cannot be split any further (the "
                        "turn-count floor), and a content-level spill of "
                        "it, where available, did not resolve this "
                        "either." + _learned_byte_limit_clause(
                            last_accepted_wire_bytes=_last_accepted_wire_bytes,
                            last_rejected_wire_bytes=_last_rejected_wire_bytes,
                        ),
                        saw_byte_limit=True,
                    )
                # Defer: move ONLY the one turn that was just attempted
                # (not the rest of raw_middle, which may still hold more
                # — the attempt size shrank to 1, not raw_middle itself)
                # into tail, the OLD stage-1 direction narrowed to this
                # one turn — a non-byte-limit compact() failure is not
                # evidence main_call itself would also fail. Any
                # remaining raw_middle stays for a later iteration,
                # attempted in full (reset to None) next time.
                tail = raw_middle[:1] + tail
                raw_middle = raw_middle[1:]
                _compact_attempt_len = None
            else:
                _compact_attempt_len = max(_current_attempt // 2, 1)
        elif _estimate_tokens_list(tail, model, use_chars4=use_chars4) > tail_min_tokens:
            # Phase 1: trim tail half → raw_middle.
            chunk = max(len(tail) // 2, 1)
            raw_middle.extend(tail[:chunk])
            tail = tail[chunk:]
        elif _estimate_tokens_list(head, model, use_chars4=use_chars4) > head_min_tokens:
            # Phase 2: trim head half → raw_middle.
            chunk = max(len(head) // 2, 1)
            raw_middle = head[-chunk:] + raw_middle
            head = head[:-chunk]
        elif _last_recover_is_byte_limit:
            # #4885: token-only shrinking is exhausted (head/tail already at
            # or below their token minimums) but the triggering cause was an
            # HTTP 413 — a BYTE limit, which those minimums say nothing
            # about. Halve the retry_loop-scoped T_max override (or start
            # from the real T_max on the first attempt) and re-derive
            # head_min/tail_min from it via `compute_budgets` — the SAME
            # function every other T_max consumer uses, called here with
            # `t_max_override` so nothing outside this local scope changes.
            _t_max_for_candidate = (
                _t_max_override if _t_max_override is not None
                else get_max_input_tokens(model)
            )
            _candidate = _t_max_for_candidate // 2
            if _candidate <= _sp_tokens_floor + _new_msg_tokens_floor:
                # Floor: SP + new_msg alone (never shrunk — see the floor
                # comment above the loop) would not fit even at this
                # candidate with head/tail/summary at zero. Halving again
                # cannot possibly succeed either (the candidate only shrinks
                # further) — stop here, not one more attempt, and name what
                # actually happened instead of the false "exceeds T_max".
                raise UnrecoveredError(
                    "retry_loop: HTTP 413 (a request-BODY-BYTE limit) "
                    "recurred even after binary-search-halving the "
                    f"in-turn token ceiling to {_candidate} tokens — SP "
                    f"({_sp_tokens_floor} tokens) and the newest message "
                    f"({_new_msg_tokens_floor} tokens) alone no longer fit, "
                    "and neither is ever shrunk. This is a request-BODY-"
                    "BYTE limit, not a token-count one — shrinking the "
                    "token-count representation further cannot resolve it "
                    "(most likely: the newest message alone exceeds the "
                    "upstream byte limit)." + _learned_byte_limit_clause(
                        last_accepted_wire_bytes=_last_accepted_wire_bytes,
                        last_rejected_wire_bytes=_last_rejected_wire_bytes,
                    ),
                    saw_byte_limit=True,
                )
            _t_max_override = _candidate
            bg = compute_budgets(
                cfg, model,
                T_SP=_sp_tokens_floor, T_comp_SP=engine._T_comp_SP,
                t_max_override=_t_max_override,
            )
            head_min_tokens = bg.head_budget
            tail_min_tokens = bg.tail_budget
            # Immediately re-check tail/head against the NEW, smaller
            # minimums and shrink in this SAME iteration if either now
            # exceeds them — without this, halving the ceiling costs one
            # iteration and shrinking content down to it costs a second
            # (main_call retried with UNCHANGED content just re-confirms the
            # same 413, wasting a turn of `max_iterations`), roughly halving
            # how many halvings fit under the safety cap for no reason: the
            # data needed to shrink (tail/head, already read above) is
            # already in hand at this exact point.
            if _estimate_tokens_list(tail, model, use_chars4=use_chars4) > tail_min_tokens:
                chunk = max(len(tail) // 2, 1)
                raw_middle.extend(tail[:chunk])
                tail = tail[chunk:]
            elif _estimate_tokens_list(head, model, use_chars4=use_chars4) > head_min_tokens:
                chunk = max(len(head) // 2, 1)
                raw_middle = head[-chunk:] + raw_middle
                head = head[:-chunk]
            # If neither exceeds the new minimums either (head/tail were
            # ALREADY below even the halved ceiling), there is nothing left
            # to trim yet — still falls through without raising; the NEXT
            # 413 (same content, same call) halves the ceiling again on its
            # own next pass through this branch, continuing the search.
        else:
            # #4954 (b): ``saw_byte_limit`` defaults to False here, and
            # this ``else`` is REACHABLE — the ``elif _last_recover_is_
            # byte_limit:`` branch above it already claims the True case,
            # so False is the only value execution can reach this point
            # carrying, not an unreachable branch where the default
            # happens not to matter.
            raise UnrecoveredError(
                "retry_loop: all shrink paths exhausted — "
                "SP + head_min + summary + tail_min + new_msg exceeds T_max"
            )

    # #4947 ②: name the byte limit when it was the LAST recovered cause —
    # a compact()-origin 413 is currently exempt from the same-cause cap
    # above (see the #4885 comment on that skip) and from every shrink
    # step in this ladder (all token-measured; a byte limit says nothing
    # about them), so it can ride every iteration to here unchanged. The
    # generic message below said nothing about WHY nothing converged;
    # this branch reports the actual last-known cause instead of leaving
    # an operator to re-derive "413" from event-log archaeology.
    # #4957: name the config key an operator can actually change here — not
    # just the bare number this call happened to be given. This is an
    # ESCAPE VALVE message, not a diagnosis: raising the cap only delays
    # exhaustion if the underlying cause never resolves (a persistent
    # 413/5xx/rate-limit keeps failing regardless of how many attempts are
    # allowed) — it does not mean the cap itself was the wrong size.
    if _last_recover_is_byte_limit:
        raise UnrecoveredError(
            f"retry_loop exceeded max_iterations={max_iterations} "
            "(chat.compaction.max_shrink_iterations) without convergence — "
            "the last recovered cause was an HTTP 413 (a request-BODY-BYTE "
            "limit), which this ladder's token-only shrink steps cannot "
            "resolve on their own. Raising this config value is an escape "
            "valve, not a cure: if the same 413 recurs every turn, it will "
            "only delay exhaustion, not prevent it." + _learned_byte_limit_clause(
                last_accepted_wire_bytes=_last_accepted_wire_bytes,
                last_rejected_wire_bytes=_last_rejected_wire_bytes,
            ),
            saw_byte_limit=True,
        )
    raise UnrecoveredError(
        f"retry_loop exceeded max_iterations={max_iterations} "
        "(chat.compaction.max_shrink_iterations) without convergence"
    )


# Keep TokenMultiplierLearner importable from this module for convenience.
# The actual implementation is in token_multiplier_learner.py.
def _get_learner_class() -> type:
    from reyn.runtime.services.token_multiplier_learner import TokenMultiplierLearner
    return TokenMultiplierLearner


__all__ = [
    "CompactionEngine",
    "ChatSummary",
    "ChatSummaryRaw",
    "ComputedBudgets",
    "CompactionBudgetSelfConsistencyError",
    "CompactionOverflowError",
    "ContextOverflowError",
    "HistoryChunkToCompact",
    "ForceCompactRaceUnrecoveredError",
    "NewMsgExceedsBudgetError",
    "UnrecoveredError",
    "assert_static_bounds",
    "compute_budgets",
    "compute_covers_through_seq",
    "estimate_tokens",
    "estimate_tokens_for_turn",
    "hard_truncate_summary",
    "retry_loop",
    "trim_head",
    "trim_tail",
]
