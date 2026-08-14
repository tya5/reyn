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
- PR-N6: ``retry_loop`` shrinks head/tail/raw_middle monotonically per iteration
  until the prompt fits or mathematical impossibility is reached.

Drop priority when over budget:
  1. body  — compaction summarises naturally
  2. head  — trim_head enforces token budget
  3. tail  — trim_tail enforces token budget
  4. SP    — dynamic SP truncate is OUT OF SCOPE for PR-N3 (separate wave)
  5. new_msg — NEVER dropped; abort + event emit (see Axis 11)
"""
from __future__ import annotations

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
    """
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
    """
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
    """

    def __init__(self, reason: str) -> None:
        self.reason = reason
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

    async def _acompletion(self, messages: list[dict], *, response_format: dict | None = None):
        """Single LLM call via the cost-observability chokepoint (#1190).

        Shared by ``compact`` (JSON response) and ``_resummarize_topic_arc``
        (text response). The chokepoint owns proxy_kwargs + provider-prefix
        strip + records usage (purpose="compaction") via the engine's recorder.
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

        response = await self._acompletion(
            messages, response_format={"type": "json_object"}
        )
        # #4703 axis①: this call's own usage, for the compaction_completed
        # marker CompactionController emits below (never a resummarize-pass
        # call's usage — those are a rare, bounded-to-1-by-default backstop;
        # the primary compact() call is the dominant cost, and capturing
        # every resummarize pass too would need threading usage out of
        # _resummarize_topic_arc as well, a disclosed follow-up, not silent
        # scope creep). None if usage genuinely could not be read off the
        # response — never coerced to 0.
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

        raw = (response.choices[0].message.content or "").strip()
        if not raw:
            raise ValueError("compaction LLM returned empty response")

        parsed: dict = loads_lenient(
            raw,
            on_raw_decode=lambda discarded_len, head: logger.warning(
                "compaction_json_raw_decode_recovered: discarded %d bytes of "
                "trailing garbage after valid JSON object. head=%r",
                discarded_len,
                head,
            ),
        )

        new_turn_seqs = parsed.get("new_turn_seqs") or []
        covers = compute_covers_through_seq(new_turn_seqs)
        if covers == 0 and input_chunk.new_turns:
            # Fallback: take max seq from the input turns directly.
            covers = max(
                (int(t.get("seq", 0)) for t in input_chunk.new_turns if isinstance(t, dict)),
                default=0,
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


# #3783 stage 2: same-cause consecutive-recover cap. Stage 3 will flip the
# default classification so more exception types recover-by-default instead
# of raising fatally; without this cap, a cause that shrinking can never fix
# (a bug misclassified as recoverable, not an actual overflow) would grind
# through all `max_iterations` LLM calls before giving up. This is a tighter,
# earlier check than `max_iterations` — it fires on the SAME cause repeating,
# not on iteration count alone, so a turn that alternates between two
# different real overflow causes is not penalised.
_MAX_CONSECUTIVE_SAME_CAUSE_RECOVERS = 2


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
) -> Any:
    """Bounded shrink loop for context overflow recovery (PR-N6).

    On success (normal path or after shrink), calls ``learner.observe`` with
    the actual vs estimated token count so the adaptive estimator learns.

    Bounded termination proof
    -------------------------
    - ``raw_middle``, ``tail``, and ``head`` each shrink monotonically per
      iteration that triggers the corresponding escalation branch.
    - Lower bounds: ``head_min = budgets.head_budget``,
      ``tail_min = budgets.tail_budget`` (derived from
      ``component_weights["head|tail"] / total_weight * main_pool``).
    - Terminal condition: when all three are at or below their minimum token
      budgets, ``UnrecoveredError`` is raised immediately.
    - ``max_iterations=8`` is a safety cap; finite-by-construction means the
      loop terminates in O(log N) shrink steps for typical sizes.
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
    from reyn.runtime.services.token_multiplier_learner import detect_content_type

    bg = engine.budgets
    head_min_tokens = bg.head_budget
    tail_min_tokens = bg.tail_budget
    use_chars4 = cfg.use_chars4_estimate

    _last_recover_cause: str | None = None
    _consecutive_same_cause = 0

    for _iteration in range(max_iterations):
        try:
            if raw_middle:
                # Compact raw_middle into the running summary.
                # Build section_token_caps from budgets.section_caps.
                section_caps = bg.section_caps if bg.section_caps else {
                    "topic_arc": 200, "decisions": 400, "pending": 400,
                    "session_user_facts": 200, "artifacts_referenced": 300,
                }
                input_chunk = HistoryChunkToCompact(
                    previous_summary=summary,
                    new_turns=raw_middle,
                    section_token_caps=section_caps,
                )
                try:
                    chat_summary = await engine.compact(input_chunk)
                    summary = chat_summary.to_dict()
                    raw_middle = []
                except Exception as exc:
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
            engine._events.emit(
                "compaction_shrink_recovered",
                cause=_cause,
                iteration=_iteration,
                consecutive=_consecutive_same_cause,
            )
            if _consecutive_same_cause > _MAX_CONSECUTIVE_SAME_CAUSE_RECOVERS:
                raise UnrecoveredError(
                    f"retry_loop: cause {_cause!r} recovered "
                    f"{_consecutive_same_cause} consecutive times (limit "
                    f"{_MAX_CONSECUTIVE_SAME_CAUSE_RECOVERS}) — shrinking is "
                    "not resolving this cause; stopping rather than "
                    "exhausting max_iterations."
                ) from _overflow_exc

        # Shrink escalation: reduce context size monotonically.
        if raw_middle:
            # Primary: move half of raw_middle into tail (= defer compaction).
            chunk = max(len(raw_middle) // 2, 1)
            tail = raw_middle[-chunk:] + tail
            raw_middle = raw_middle[:-chunk]
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
        else:
            raise UnrecoveredError(
                "retry_loop: all shrink paths exhausted — "
                "SP + head_min + summary + tail_min + new_msg exceeds T_max"
            )

    raise UnrecoveredError(
        f"retry_loop exceeded max_iterations={max_iterations} without convergence"
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
