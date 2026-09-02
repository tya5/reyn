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
import enum
import hashlib
import json
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Union

from reyn.llm.json_parse import loads_lenient
from reyn.llm.litellm_bootstrap import (
    LitellmWarmingInBackgroundError,
    ensure_litellm_ready_or_defer,
)
from reyn.prompt import compaction as _prompt_compaction
from reyn.runtime.error_format import is_quota_exhausted_error
from reyn.runtime.session_pure import render_summary_for_storage

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


class SeqUnavailable(enum.Enum):
    """#5475 (architect ruling): named reasons a caller of :meth:`CompactionEngine.
    compact` cannot supply a real ``covers_through_seq`` for its
    ``compaction_started`` audit event.

    A bare ``None`` was explicitly rejected — a consumer of the event cannot
    tell "seq is absent" apart from "seq is unknown for a structural reason",
    the same ambiguity architect named twice the same day (#5084/#5132) with
    the same prescription: make the absence itself carry WHY, not just THAT.
    """

    #: `retry_loop`'s own internal compaction (`engine.py`'s own later
    #: `engine.compact(input_chunk)` call, inside the halving/spill ladder)
    #: builds `new_turns` from `RouterHistoryBuffer.decompose_history_for_
    #: retry()`'s `raw_middle` — litellm WIRE dicts (`_serialise_turn`'s own
    #: output), which structurally carry no `seq` field at all (that method's
    #: own `seq_by_id` side-channel, keyed by `id(wire_dict)`, exists
    #: specifically because the wire shape has no room for one). Threading
    #: `seq_by_id` through to this call site was considered and rejected
    #: (architect): an `id()`-keyed lookup silently breaks across any
    #: copy/re-serialise, extending a fragile mechanism's lifetime rather
    #: than fixing it.
    WIRE_DICTS_CARRY_NO_SEQ = "wire_dicts_carry_no_seq"


#: The `compaction_started` payload's `covers_through_seq` field: either a
#: real seq (the controller's own caller, whose `new_turns` DO carry one —
#: see `_turn_to_compactor_input`), or a named reason it cannot be supplied.
#: No default anywhere `compact()` is called — an omitted value is a
#: caller writing a payload it cannot actually back, caught by mypy at the
#: call site, never a silently-accepted null.
CoversThrough = Union[int, SeqUnavailable]


#: The ``role`` value marking an element of :attr:`HistoryChunkToCompact.
#: messages` as an already-compacted summary rather than an ordinary turn
#: — condition⑤'s own discriminator (#5531), matching the SAME convention
#: the persisted-history layer already uses (a ``ChatMessage.role ==
#: "summary"`` entry in ``history.jsonl``). One vocabulary, not two.
#:
#: #5598: this is reyn's OWN internal vocabulary — a discriminator
#: `watermark`/`trim`/`spill` logic reads (``router_history_buffer.py``,
#: ``router_loop_driver.py``, this module's own ``compact()``), never a
#: value a provider recognises as a chat role. Every genuine wire-egress
#: point must run it through :func:`wire_role` before it reaches
#: ``loop.run``/litellm — see that function's own docstring for the
#: incident this closes.
SUMMARY_MESSAGE_ROLE = "summary"


def wire_role(role: str) -> str:
    """#5598 (owner's real machine, 2026-08-30) — maps reyn's own internal
    role vocabulary to the value a provider actually accepts on the wire.
    Two internal roles have no provider equivalent:

    - ``"agent"`` — a legacy pre-migration straggler
      (``router_history_buffer.py``'s own pre-existing normalize-on-read).
    - :data:`SUMMARY_MESSAGE_ROLE` (``"summary"``) — reyn's own
      discriminator for an already-compacted summary turn, read by
      watermark/trim/spill logic, never a provider role. Left un-mapped
      at the wire boundary, a provider that validates role names against
      a fixed enum (the incident: `gpt-5.6-luna`, "Invalid value:
      'summary'. Supported values are: 'assistant', 'system', 'developer',
      and 'user'.") rejects the request outright, with a 400 that fires
      in ~2 seconds regardless of payload size — the request never
      reaches inference at all, so no amount of shrinking (compaction,
      spill, any rung of the overflow ladder) can ever recover from it.
      This is the SAME turn that just successfully summarised: the very
      next request that includes that summary is the one that 400s.

    Both collapse to ``"assistant"`` — a summary's own content already
    self-identifies via its ``"[summary of earlier conversation]\\n"``
    prefix (:func:`wrap_summary_as_message`'s own decoration), so the
    role does not need to carry that information a second time.

    Deliberately NOT applied inside :func:`wrap_summary_as_message`
    itself, nor anywhere :data:`SUMMARY_MESSAGE_ROLE` is used to build
    :class:`HistoryChunkToCompact` (``compaction_controller.py``'s own
    call site, ``retry_loop``'s own ``raw_middle`` inclusion) or read
    back by ``compact()``'s own "does a previous summary already exist"
    check (this module, ``m.get("role") == SUMMARY_MESSAGE_ROLE``) —
    those are reyn's OWN internal representation, never wire-serialised
    as individual role-tagged messages (``compact()``'s own LLM call
    embeds the whole ``messages`` list as JSON TEXT inside one
    ``"user"``-role wire message, never as separate wire roles) —
    normalizing there would break the very discriminator this function's
    own docstring says stays internal. Apply this ONLY at a genuine
    wire-egress point, immediately before a dict becomes part of what
    ``loop.run``/litellm actually receives — see
    ``RouterHistoryBuffer._serialise_turn`` and
    ``RouterLoopDriver._router_main_call`` for the two such points."""
    if role in ("agent", SUMMARY_MESSAGE_ROLE):
        return "assistant"
    if role not in ("user", "assistant", "tool", "system"):
        raise ValueError(f"unknown wire role: {role!r}")
    return role


def wrap_summary_as_message(summary: dict) -> dict:
    """#5531 condition④ — a previously-compacted summary is JUST a message
    in :attr:`HistoryChunkToCompact.messages`, distinguished only by
    :data:`SUMMARY_MESSAGE_ROLE`. This is the ONE place that wrapping
    happens, so every caller (the controller's tail-side path, retry_loop's
    tail- and head-side paths, ``RouterHistoryBuffer._serialise_turn``'s
    own summary branch) produces byte-identical shape — see
    ``CompactionEngine.compact``'s own docstring for why the field carries
    no separate identity beyond this ``role`` marker.

    #5531 (lead-coder ruling, option (y), issuecomment-5463125441): also
    carries a rendered ``content`` string — the SAME "[summary of earlier
    conversation]\\n" + text shape ``_router_main_call`` used to splice in
    separately. Two reasons this lives HERE, not at each call site:
    (1) ``estimate_tokens_for_turn``/``estimate_tokens_for_any_turn``
    (this module) read ``content``/``text`` — a dict with neither falls
    through to an empty-string estimate, so an un-rendered summary would
    measure as near-zero and could legitimately get discarded by
    ``trim_head`` once budget is exceeded elsewhere, silently dropping it
    from the wire; (2) it lets every site that places a summary directly
    into a message list (this function's own callers) skip carrying a
    SEPARATE decoration step — the content IS the decoration, so there is
    no second place a caller could decide whether/how to splice it in."""
    rendered = render_summary_for_storage(summary)
    return {
        "role": SUMMARY_MESSAGE_ROLE,
        **summary,
        # Always wins over any stray "content" key `summary` itself might
        # carry — this rendered form is the ONE the wire actually sends.
        "content": f"[summary of earlier conversation]\n{rendered}",
    }


@dataclass
class HistoryChunkToCompact:
    """Input to the compaction engine.

    #5531 (owner design dialogue, invariant: a summary represents ONE
    continuous span, placed exactly where that span sat in time):
    ``messages`` is a SINGLE ordered list, oldest-first — the true
    chronological sequence to compact. An already-compacted summary is
    just one element of it (condition④), marked with
    :data:`SUMMARY_MESSAGE_ROLE`, never assumed to sit at either end —
    replaces the OLD ``previous_summary``/``new_turns`` two-field shape,
    which could only express "previous_summary, then new_turns" (always
    append) and had no way to represent content added from the OLDER
    side (retry_loop's own head-shrink path, #5531 condition C's real
    consequence — see that issue's own investigation comment for the
    trace). Build via :func:`wrap_summary_as_message` for the summary
    element, never a hand-rolled dict, so every caller uses the same
    marker.

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
    messages: list[dict]                            # ordered, oldest-first: turns + (usually) at most one role=="summary" element — but see #5531: history CAN hold more than one summary turn (an untouched original + a fresh fold's own output), so >1 can appear here if more than one falls inside the offered span
    section_token_caps: dict                        # {topic_arc, decisions, ...} — LLM hint only, see docstring above


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
    unaffected either way; #5577 moved ``_router_main_call``'s own except,
    router_loop_driver.py, off calling this function directly onto
    ``classify_llm_failure`` instead — quota still excluded either way,
    since that function's own RETRYABLE branch calls THIS SAME
    ``is_quota_exhausted_error`` check, just one level up). Checked here,
    at the single shared predicate, rather than adding a guard at each of
    its call sites individually — #5329's own reason to exist is exactly a
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


class LLMFailureClass(enum.Enum):
    """#5543 / #5531 §10 — the ONE three-way classification the shrink
    ladder is allowed to branch on. Every ``retry_loop`` overflow-recovery
    site must classify through this before deciding what to do with an
    exception; the three members are jointly exhaustive and mutually
    exclusive over ``classify_llm_failure``'s own domain.

    FATAL:
        A bug in reyn's OWN code (``TypeError``/``AttributeError``/
        ``KeyError``) or a misconfigured credential (auth error). Never
        shrunk — re-raised immediately. Owner (#3783, verbatim): "An
        ``AttributeError`` in our own code must not become 'quietly
        shrink, then ``UnrecoveredError``'" — shrinking a Fatal exception
        burns real LLM calls chasing a cause no amount of shrinking can
        fix, then reports the WRONG diagnosis (context-overflow) for a
        code defect.
    RETRYABLE:
        An infrastructure condition (5xx / timeout / connection failure),
        a per-request rate limit, or a usage-quota exhaustion — never
        shrunk either; the fix is waiting, not sending less. Routed
        through the SAME backoff machinery the router already uses
        (``llm.py``'s ``_llm_call_with_retry``) rather than the shrink
        ladder.
    OVERFLOW:
        A genuine context/body-size overflow (token-count or byte-limit).
        The ONLY class that enters the shrink ladder — shrinking the
        input is the correct remedy precisely because the input's SIZE
        is the cause.

    Precedence, in the order ``classify_llm_failure`` checks them: FATAL
    first (a code bug's own class name is a stronger, narrower signal
    than any provider-shaped heuristic below it — an ``AttributeError``
    from provider glue code must not fall through to a keyword match),
    then RETRYABLE (an infra/rate-limit/quota condition is a definitive
    provider signal, checked before the broader OVERFLOW keyword
    fallback), then OVERFLOW last (the widest, string-fallback-backed
    check — see ``is_context_overflow_error``'s own docstring on why its
    keyword fallback must never be the ONLY signal consulted, and why it
    therefore runs after the narrower checks above it, not before).
    """

    FATAL = "fatal"
    RETRYABLE = "retryable"
    OVERFLOW = "overflow"


#: #5543 (owner ruling, #3783 §2's own "AttributeError must not become
#: quiet-shrink-then-UnrecoveredError" requirement): the closed allowlist
#: of reyn's-own-code-bug exception types. Deliberately NOT "any exception
#: type we don't recognise" (that would make FATAL the default for a
#: genuinely novel provider exception shape, the opposite of this
#: classification's own safe-side posture — an unrecognised shape should
#: fall through to OVERFLOW's keyword fallback, not be treated as
#: unshrinkable-and-fatal) — only the THREE types a bug in reyn's own
#: code plausibly raises through this call path.
#:
#: #5536 group C: made PUBLIC (was ``_FATAL_EXC_TYPES``) so
#: ``reyn.hooks.shell_runner``'s own outer catch-all can reuse the SAME
#: allowlist to exclude reyn's-own bugs from its best-effort "the hook
#: run failed" catch, rather than inventing a second, divergent set (the
#: exact "5 independent copies" failure class ``is_context_overflow_
#: error``'s own docstring already names once for a sibling predicate).
FATAL_EXC_TYPES: "tuple[type[BaseException], ...]" = (TypeError, AttributeError, KeyError)


def _is_fatal_auth_error(exc: BaseException) -> bool:
    """True for a credential/permission failure — the auth bucket
    ``reyn.runtime.error_format._bucket_for`` already names (kept as a
    separate, narrower check here rather than importing that function
    directly: this module intentionally does not depend on the
    user-facing formatter, only on the SAME underlying signal it reads
    — class name substring or a 401/403 status code)."""
    name = type(exc).__name__
    code = getattr(exc, "status_code", None)
    return "Authentication" in name or "PermissionDenied" in name or code in (401, 403)


def classify_llm_failure(exc: BaseException) -> LLMFailureClass:
    """#5543 / #5531 §10 — classify an exception raised from an LLM call
    (main_call or ``compact()``) into exactly one of :class:`LLMFailureClass`'s
    three members, so the shrink ladder can branch on ONE unified
    classification instead of ad-hoc, per-site predicate calls (the shape
    #5531 §10 names as the reason ``max_iterations`` alone is not a safe
    substitute for a real classification: removing the iteration cap
    WITHOUT this function first would let a FATAL bug — a plain
    ``AttributeError`` in reyn's own code — get quietly shrunk through the
    entire ``T_max``-halving floor, burning real LLM calls, before finally
    failing with the wrong diagnosis).

    Checked in this order, each narrower/more-definitive-first (see
    :class:`LLMFailureClass`'s own docstring for why this order is not
    arbitrary):

    1. FATAL — an exact ``isinstance`` match on reyn's own closed bug-type
       allowlist, or an auth-error shape (class name / status code).
    2. RETRYABLE — ``is_quota_exhausted_error`` (a provider usage-window
       exhaustion, #5256), the SAME infra/rate-limit signal
       ``llm.py``'s own ``_llm_call_with_retry`` already retries
       (5xx / timeout / connection failure / rate-limit-429), checked
       WITHOUT importing that module (this function must not create a
       ``compaction`` → ``llm`` import cycle — the two modules' retry
       machinery stays two callers of the SAME classification, not one
       importing the other's private helper) — OR (#5568, below)
       ``status_code == 200``, checked BEFORE ``is_context_overflow_
       error``'s own keyword-string fallback ever runs (that fallback
       lives one level up, in ``router_loop_driver._is_shrinkable_
       overflow`` — it is only ever reached for a cause this function
       itself classified OVERFLOW, so returning RETRYABLE here is what
       keeps this cause out of it).
    3. OVERFLOW — ``is_context_overflow_error`` (this module's own,
       already-shared predicate — 413 / token-length signals, with a
       keyword-string fallback for a flattened provider exception).

    An exception matching none of the three falls through to OVERFLOW —
    the pre-#5543 default this ladder already had (``retry_loop``'s own
    except clause only ever catches ``CompactionOverflowError``/
    ``ContextOverflowError`` today, both already overflow-shaped by
    construction) — this function does not widen what reaches it, only
    names what was already implicitly assumed.

    #5568 (owner's real-machine incident, reyn-self ``coder-brown``):
    litellm's ``OpenAIResponsesAPIConfig.transform_response_api_response``
    (the class reyn's own ``provider=openai`` config resolves to when
    talking to a local proxy — NOT the ``ChatGPTResponsesAPIConfig``
    #5603(B) patches, confirmed unreached in production via a reach-marker
    with 0 matching lines) does ``raw_response.json()`` inside a bare
    ``try/except Exception`` and, on failure, raises ``OpenAIError(message=
    raw_response.text, status_code=raw_response.status_code)`` — an HTTP
    200 (the request DID succeed at the transport layer) wrapping the
    ENTIRE raw response body (in the observed incident, a raw SSE stream
    the upstream proxy returned despite ``stream: false``) as the
    exception's own message. Because that body can coincidentally contain
    an overflow-shaped keyword (an ``error`` frame's own text), the
    pre-#5568 fallthrough to OVERFLOW let this reach
    ``is_context_overflow_error``'s keyword fallback and enter the shrink
    ladder — repeatedly halving and re-sending a 9M-character history
    against a cause no amount of shrinking can fix (a transport/protocol
    failure, not an input-size one; ADR-0044 I2: "do not apply an
    irreversible remedy to a reversible cause" — waiting/retrying is
    reversible, shrinking permanently discards conversation content).

    The honest predicate is ``status_code == 200`` alone — NOT also
    checking whether the message parses as JSON. litellm's own exception
    ``__str__``/``.message`` always carries a class-name prefix (e.g.
    ``"litellm.APIError: <body>"``, confirmed directly against a real
    ``litellm.APIError`` instance), so a check like ``json.loads(str(exc))``
    would ALWAYS fail regardless of whether the underlying body was JSON
    or not — measuring nothing beyond what ``code == 200`` already
    measures, while giving the false impression of a narrower, message-
    content-aware check. ``code == 200`` alone is also sufficient by
    construction: ``transform_response_api_response`` raises status 200
    ONLY on this exact "transport succeeded, response-object conversion
    failed" shape (architect's own trace) — no separate message-content
    check is needed to confirm that, and checking the private prefix
    string instead (architect's own rejected option (ii)) would repeat
    #5603's own mistake of depending on a private, version-fragile
    litellm implementation detail.

    Disclosed tradeoff (architect's own ruling, kept here deliberately):
    in a still-broken-proxy world, a REAL overflow signal (an SSE
    ``error`` frame reading "exceeds the context window") can be buried
    inside a 200-status exception's own body — this predicate classifies
    that RETRYABLE too, not OVERFLOW. This is correct, not a gap:
    shrinking is not a repair for "the proxy returned SSE for a
    stream: false request" (the real incident kept producing the same
    200+SSE shape even after shrinking a 9M-character history down to
    18K), and once the proxy is fixed (#5568's own separate "A"), a
    genuine overflow reaches this function as a real 4xx
    ``ContextWindowExceededError`` and classifies OVERFLOW exactly as
    before. The way back to that world is fixing the proxy, never
    compensating for it in classification — compensating would make the
    proxy's own contract violation permanently invisible (Q3: does the
    repair destroy the evidence).

    architect's own ruling (issue #5568): the root cause is the upstream
    proxy's contract violation (owner's own hand, a separate fix, layered
    ABOVE the provider per the owner's standing "reyn fights above the
    provider layer" principle) — reyn's own correction is classification
    only, never teaching litellm/reyn to accept the malformed response
    (that would make the proxy's own defect permanently invisible, the
    exact Q3 "does the repair destroy the evidence" band violation).
    """
    if isinstance(exc, FATAL_EXC_TYPES) or _is_fatal_auth_error(exc):
        return LLMFailureClass.FATAL
    if is_quota_exhausted_error(exc):
        return LLMFailureClass.RETRYABLE
    name = type(exc).__name__
    code = getattr(exc, "status_code", None)
    if "RateLimit" in name or code == 429:
        return LLMFailureClass.RETRYABLE
    if (
        "Timeout" in name
        or "Connection" in name
        or "ConnectError" in name
        or "ServiceUnavailable" in name
        or "BadGateway" in name
        or "InternalServerError" in name
        or (isinstance(code, int) and 500 <= code < 600)
    ):
        return LLMFailureClass.RETRYABLE
    # #5568 (architect's own honest predicate, PR #5614 review): an HTTP
    # 200 (the request genuinely succeeded at the transport layer) on an
    # EXCEPTION is a protocol/transport failure, unconditionally — never
    # an input-size question — so it must never reach the OVERFLOW
    # keyword fallback below. `status_code == 200` alone, deliberately
    # NOT also checking whether the message parses as JSON: litellm's own
    # exception `__str__`/`.message` always carries a class-name prefix
    # (e.g. `"litellm.APIError: <body>"`), so a `json.loads(str(exc))`
    # check would ALWAYS fail regardless of the underlying body — an
    # earlier version of this branch had exactly that check, which
    # measured nothing beyond `code == 200` already does (lead-coder's
    # own catch, PR #5614 review, confirmed directly against a real
    # `litellm.APIError`). See this function's own docstring for the
    # disclosed tradeoff (a genuine overflow signal buried in a 200
    # exception's body also classifies RETRYABLE here — correct, not a
    # gap) and why the private prefix string is deliberately NOT checked
    # either (#5603's own "depended on a private, version-fragile litellm
    # detail" mistake, not repeated here).
    if code == 200:
        return LLMFailureClass.RETRYABLE
    return LLMFailureClass.OVERFLOW


def is_shrinkable_overflow(exc: BaseException) -> bool:
    """#5577/#5593/#5622 — is *exc* a cause the shrink ladder should be
    entered for? The predicate ``router_loop_driver.py``'s own 2 call
    sites share (imported from here, never duplicated — the relocation
    #5622 (issue) actually landed).

    ``classify_llm_failure``'s own fallthrough is unconditionally
    ``OVERFLOW`` for anything that is neither FATAL nor RETRYABLE — a
    default calibrated for THIS module's own ``retry_loop`` inner
    except clause, which (per that call site's own comment, below)
    "only ever catches CompactionOverflowError/ContextOverflowError
    today, both already overflow-shaped by construction" for the
    ORIGINAL caller (``compact()``'s own exception, always genuinely
    overflow-shaped — the one call site where ``classify_llm_failure``
    alone is actually safe, and where it deliberately stays, per
    #3783's own owner-ratified default: "only exceptions that make
    compaction impossible to continue should propagate; the default
    should be recover" — see that call site's own comment for the
    full trace of why #5622 (issue)'s "1 unified discriminator across
    all 3 sites" prescription was NOT adopted there).

    This function's own 2 callers (``router_loop_driver.py``, NOT
    ``retry_loop`` here) catch a WIDER exception surface — ANY
    exception the router/provider stack can raise from
    ``loop.run()`` — including one neither FATAL, RETRYABLE, nor an
    overflow at all (#5593's real incident:
    ``StructuredOutputUnsupportedModelError`` — not in
    ``FATAL_EXC_TYPES``, not a rate-limit/timeout/5xx/quota shape, so
    ``classify_llm_failure``'s fallthrough classified it OVERFLOW,
    wrapped it, and the shrink ladder burned real LLM calls on a cause
    no amount of shrinking could ever fix, then reported the wrong
    diagnosis — ``UnrecoveredError``, out of context, for a config
    error).

    Fix: still exclude FATAL/RETRYABLE via ``classify_llm_failure``
    (#5577's own gain — a quota/5xx/timeout exception whose message text
    merely resembles an overflow keyword must not enter here), but for
    anything ``classify_llm_failure``'s OWN 3-way split does not itself
    prove is FATAL or RETRYABLE, require the STRONGER, narrower
    ``is_context_overflow_error`` signal too (litellm's typed
    ``ContextWindowExceededError``, a 413, or an overflow keyword) —
    restoring the pre-#5577 conservative default (unmatched shape =
    False, do not enter) for ``router_loop_driver.py``'s own 2 call
    sites — the surface ``classify_llm_failure``'s bare fallthrough
    was never designed to answer for on its own."""
    failure_class = classify_llm_failure(exc)
    if failure_class in (LLMFailureClass.FATAL, LLMFailureClass.RETRYABLE):
        return False
    return is_context_overflow_error(exc)


class CompactionOverflowError(Exception):
    """The compaction LLM call itself exceeded its B_M budget.

    Raised when the compaction call (= the inner ``engine.compact()`` call
    inside retry_loop) returns a context-length error.  Triggers the same
    escalation path as ContextOverflowError: shrink raw_middle/tail/head and
    retry.
    """


class RetryLoopTerminal(enum.Enum):
    """#5531 §10 / ADR-0044 — WHICH of retry_loop's two terminal predicates
    raised, as a structured value a caller can switch on without parsing
    ``UnrecoveredError.reason`` (message wording is not a stable API —
    #4948/#4957's own reason for ``saw_byte_limit`` existing as a real
    field rather than a string to grep; this is the SAME argument applied
    to the OTHER axis the doc named still unmet: "which terminal", not
    "was it byte-limited").

    MID_FLOOR:
        Terminal (a) — ``raw_middle`` is down to one turn, that turn has
        already been offered to spill (and either had no spillable
        content or spilling it did not resolve the overflow), and halving
        the offered slice further cannot produce a smaller nonzero size.
    ROOM_FLOOR:
        Terminal (b) — the T_max-halved candidate can no longer fit
        ``SP`` + ``new_msg`` + the current summary even with ``head``/
        ``tail`` at zero; none of the three is ever shrunk by this
        ladder, so halving again cannot possibly help either.

    Deliberately NOT reused as/with ``saw_byte_limit`` — that field is a
    different axis (whether a byte limit was OBSERVED during this call's
    shrink attempts, "last seen not sticky") from this one (WHICH
    predicate the raise itself satisfies); the two are independent and a
    single raise carries exactly one value of each.
    """

    MID_FLOOR = "mid_floor"
    ROOM_FLOOR = "room_floor"


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
    terminal:
        #5531 §10 / ADR-0044 (owner: "don't make the doc follow a wrong
        implementation" — the doc's own claim, chat-compaction.md's
        "travels as a structured value, not as a distinct exception
        type", is the ratified spec; this field is what makes it true).
        Which of :class:`RetryLoopTerminal`'s two members this raise is —
        set at every raise site, never defaulted or inferred after the
        fact.
    saw_byte_limit:
        #4954 (b), architect finding: whether an HTTP 413 (a
        request-BODY-BYTE limit) was observed during this call's shrink
        attempts — a real structured field, not something a caller
        should re-derive by string-matching ``reason`` (message wording
        is not a stable API; #4948/#4957 named the byte limit in prose
        for a HUMAN operator, not for a caller to parse).

        Deliberately named ``saw_byte_limit``, not ``is_byte_limit`` (or
        anything implying "this raise's own root cause"): #5531 §10
        retired both the same-cause cap and ``max_iterations``
        exhaustion (the two raise sites this comment used to name as
        NOT determining the cause themselves) — the only two raise
        sites left are the mid=1-turn floor and the T_max binary-search
        floor, and BOTH are byte-limit-gated in their own message/
        ``saw_byte_limit`` value (mode-independent since #5531 §3 item
        12 — the value here is genuinely "was the LAST recovered cause
        this call a byte limit", true of the raise itself at either
        site, not merely a residual observation from an earlier,
        unrelated branch). A name like ``is_byte_limit`` still invites
        a future reader to assume something even stronger (that EVERY
        recover this call ever saw was byte-limited, not just the
        last) — the "last observed, not sticky" distinction below is
        the reason the field stays named this way.

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

    def __init__(
        self, reason: str, *, terminal: RetryLoopTerminal, saw_byte_limit: bool = False,
    ) -> None:
        self.reason = reason
        self.terminal = terminal
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
            # #5582 (owner proposal, 2026-08-30 — "compact はつねに stream
            # false にする対応"): this call never passed a stream_override
            # at all before this line, which lands on _streaming_enabled's
            # own override=None branch — catalog-driven, defaults to
            # streaming. Streaming buys nothing here: this call produces
            # ONE summary, never passes on_content_delta, and nobody
            # observes a delta from it — while compaction is itself one of
            # retry_loop's two overflow-ladder entry points (#5531 §9.6),
            # so a stream this call doesn't need can misdiagnose the SAME
            # ladder that is supposed to recover it (#5581's own shape).
            # Literal here, not a new purpose-keyed default mechanism —
            # this is the one call site that needs it.
            stream_override=False,
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

    async def compact(
        self, input_chunk: HistoryChunkToCompact, *, covers_through: CoversThrough,
    ) -> ChatSummary:
        """Run one compaction LLM call and return a ChatSummary.

        Axis 9: applies hard_truncate_summary to the returned topic_arc
        to ensure the body ≤ body_budget tokens deterministically.

        Raises on LLM error; callers wrap in try/except and emit
        ``compaction_failed`` if needed.

        #5475 (architect ruling): emits ``compaction_started`` HERE, at the
        one real entry both of this engine's callers share
        (``CompactionController.force_compact_now`` and ``retry_loop``'s own
        internal compaction attempts) — moved from ``CompactionController``,
        not duplicated (the controller's own former emit is deleted in the
        same change; see #5382/#5455 for why two emit sites for the same
        kind is the shape this repo rejects). ``new_turn_count``/
        ``had_previous`` are derived from *input_chunk* alone — both are
        genuinely available to every caller. ``covers_through`` is NOT: the
        controller's own caller can supply a real seq (its ``new_turns``
        carry one), but ``retry_loop``'s cannot (its ``new_turns`` are wire
        dicts with none) — a REQUIRED keyword-only argument, no default,
        so the type checker catches an omission at the call site rather
        than this method silently accepting a null it cannot justify.

        #5475 (architect, non-blocking): the emitted ``compaction_started``
        payload's ``covers_through_seq``/``covers_through_unavailable_
        reason`` fields are a PAIR, meant to be read together — JSON has
        no union type, so the ``int | SeqUnavailable`` distinction this
        method's own type signature carries has to split across two
        fields on the wire. A consumer reading only ``covers_through_seq``
        still sees a bare, unexplained ``null`` on the retry_loop path;
        always check ``covers_through_unavailable_reason`` alongside it.
        """
        # #5531: new_turn_count/had_previous are now derived from the
        # single ordered `messages` list — every "summary" element (there
        # can be more than one — see HistoryChunkToCompact's own
        # docstring) is not a "new turn" being summarised for the first
        # time. The list comprehension below already counts however many
        # there are; nothing here assumes exactly one.
        summary_messages = [
            m for m in input_chunk.messages if m.get("role") == SUMMARY_MESSAGE_ROLE
        ]
        new_turn_count = len(input_chunk.messages) - len(summary_messages)
        covers_through_seq = covers_through if isinstance(covers_through, int) else None
        covers_through_unavailable_reason = (
            None if isinstance(covers_through, int) else covers_through.value
        )
        # #5592 (owner ruling, "事後で測れる情報を残して" — post-hoc-
        # measurable, verbatim): the ACTUAL size of what this call is
        # about to send, not an estimate. `new_turn_count` above answers
        # "how many turns" — it does NOT answer "how much text", and
        # #5592's own incident (lead-coder misread `new_turn_count` as a
        # shrink signal, for lack of a real size field, and had to fall
        # back to `ls -l history.jsonl` to answer the question this field
        # exists to answer directly) is exactly the gap this closes.
        # Char count of the exact JSON this call sends — not a token
        # estimate (owner's own standing instruction: never build logic
        # on an unverified estimate; a real character count needs no
        # tokenizer and is exact by construction, unlike litellm.
        # token_counter's own approximation).
        input_chars = len(json.dumps(input_chunk.messages, ensure_ascii=False))
        self._events.emit(
            "compaction_started",
            new_turn_count=new_turn_count,
            covers_through_seq=covers_through_seq,
            covers_through_unavailable_reason=covers_through_unavailable_reason,
            had_previous=bool(summary_messages),
            input_chars=input_chars,
        )

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

        # #5531: `messages` (the single ordered list, condition④) is the
        # whole wire payload — no separate previous_summary/new_turns
        # keys to keep in sync with each other (that split is exactly
        # what made B's append-only prompt framing unfixable per-call:
        # 2 fields cannot express "which side did the new content come
        # from").
        user_content = json.dumps({
            "messages": input_chunk.messages,
            "section_token_caps": input_chunk.section_token_caps,
        }, ensure_ascii=False)

        llm_messages = [
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
        reprompt_messages = list(llm_messages)
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
        #
        # #5498 (architect ruling, on this exact call site): for
        # retry_loop's own caller, ``input_chunk.messages``' turn elements
        # (#5531 renamed the field from ``new_turns``) are litellm
        # wire dicts with no ``seq`` key at all (see ``SeqUnavailable.
        # WIRE_DICTS_CARRY_NO_SEQ``'s own docstring) — every ``t.get("seq",
        # 0)`` above falls to its default, so ``covers`` is structurally
        # 0 on that path. Confirmed harmless by TWO independent facts, not
        # one:
        #   (1) #5612 (architect co-vet, PR #5617): retry_loop's own
        #       summary IS persisted now, per successful fold
        #       (``on_summary_used`` -> ``CompactionController.
        #       persist_recovery_summary``, router_loop_driver.py) — the
        #       "never persists" claim this comment used to make here is
        #       no longer true. The 0 computed at THIS call site is still
        #       harmless, but for a DIFFERENT, still-live reason: the
        #       persist-time caller never reads this value at all — it
        #       re-derives its own real ``covers_through_seq`` from
        #       ``decompose_history_for_retry``'s own ``seq_by_id`` map
        #       (the actual folded turns' real seqs,
        #       ``max(..., default=0)`` — router_loop_driver.py's own
        #       ``_on_recovery_summary_used``) — and even a bogus 0 that
        #       DID somehow reach ``persist_recovery_summary`` is rejected
        #       outright there (``if covers_through_seq <= 0: ... return``,
        #       never appended — ``compaction_controller.py``).
        #   (2) for the OTHER caller (CompactionController), a real 0
        #       here would ALSO be masked — ``compaction_controller.py``'s
        #       own ``covers = chat_summary.covers_through_seq or
        #       candidates[-1].seq`` falls back to a real seq whenever
        #       this value is falsy. That ``or`` was written for a
        #       DIFFERENT reason (a wrong/empty LLM echo, #4951-A) — it
        #       was never intended as a defense against THIS 0, but it
        #       happens to also BE one; do not remove it.
        # Both facts are load-bearing SEPARATELY — either one alone would
        # still make this 0 harmless today, so a future change that
        # removes only one of them (e.g. starts persisting retry_loop's
        # own summary) needs to re-derive whether the other still holds
        # before assuming this is still safe. See
        # tests/services/test_5498_retry_loop_covers_zero_never_persisted.py.
        # #5531: derive from `messages` MINUS every summary element (there
        # can be more than one — see HistoryChunkToCompact's own
        # docstring) — none of their own covers_through_seq values are a
        # "new turn" seq, and each may sit anywhere in the ordered list
        # now (condition④), not just at index 0. The filter below already
        # excludes all of them, not just the first.
        covers = compute_covers_through_seq(
            [
                t.get("seq", 0) for t in input_chunk.messages
                if isinstance(t, dict) and t.get("role") != SUMMARY_MESSAGE_ROLE
            ]
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


def _summary_tokens_in(
    *lists: list[dict],
    model: str,
    use_chars4: bool = False,
) -> int:
    """#5531 PR-2: sum the token estimate of every ``role==SUMMARY_MESSAGE_
    ROLE`` element found across the given wire-dict lists (``head``/``tail``
    — never ``raw_middle``, which is not part of what ``main_call`` sends).

    Recomputed FRESH every call, never memoized like SP/new_msg's own
    floors below — unlike those two fixed parameters, a summary's size
    genuinely changes mid-call (a successful fold replaces it with a
    smaller, newer one; owner's own invariant 5: "the reservation is then
    recomputed"). More than one element can match (history can hold more
    than one summary turn — see ``HistoryChunkToCompact``'s own
    docstring) — this sums all of them, never assumes exactly one."""
    return sum(
        estimate_tokens_for_turn(t, model, use_chars4=use_chars4)
        for lst in lists
        for t in lst
        if isinstance(t, dict) and t.get("role") == SUMMARY_MESSAGE_ROLE
    )


def _has_non_summary(items: list[dict]) -> bool:
    """#5531 PR-2: whether ``items`` holds at least one NON-summary
    element — Phase 1/2's own trigger condition (below) must check this
    alongside "exceeds budget", not just the token count alone: once a
    list is ALL summary (reserved, ``_split_off_non_summary`` skips it
    entirely), the token check alone stays True forever even though
    NOTHING can actually be moved — that starvation kept the reservation
    ladder's own floor-check from ever being reached (Phase 1/2 kept
    "handling" the overflow, making zero progress, every iteration)."""
    return any(
        not (isinstance(t, dict) and t.get("role") == SUMMARY_MESSAGE_ROLE)
        for t in items
    )


def _split_off_non_summary(
    items: list[dict], count: int, *, from_end: bool,
) -> "tuple[list[dict], list[dict]]":
    """#5531 PR-2 (owner ruling, issuecomment-5465590083): Phase 1/2's own
    window-shrink must not pull a ``role==SUMMARY_MESSAGE_ROLE`` element
    into ``raw_middle`` — a summary is RESERVED (owner's own invariant 5),
    and that reservation must hold structurally in the ONE place actual
    window-shrinking happens, not just in the candidate/room arithmetic.
    Without this, a summary sitting at the boundary Phase 2 pulls from
    (``head``'s own end) or Phase 1 pulls from (``tail``'s own start) gets
    pulled back into raw_middle immediately after a fold just placed it
    there, re-folds it, places it back — an observed oscillation
    (test_413_recovery_succeeds_once_binary_search_lowers_t_max_enough
    never converged before this fix).

    Returns ``(removed, kept)`` — ``count`` NON-summary elements taken
    from the end (``from_end=True``, Phase 2/head) or start
    (``from_end=False``, Phase 1/tail) of ``items``, skipping over (never
    removing) any summary element; each list preserves the original
    relative order of what it holds. If fewer than ``count`` non-summary
    elements exist, takes as many as there are (never raises) — the
    caller's own ``max(..., 1)`` chunk-sizing already handles "nothing
    left to take" by making no progress — #5531 §10 (see ``retry_loop``'s
    own "Bounded termination proof" docstring): a Phase that makes no
    progress here falls through to the OTHER branches (spill, or the
    T_max-halving floor), which do still strictly decrease the measure,
    so this alone stalling never stalls the whole ladder.
    """
    non_summary_idx = [
        i for i, t in enumerate(items)
        if not (isinstance(t, dict) and t.get("role") == SUMMARY_MESSAGE_ROLE)
    ]
    take = set(non_summary_idx[-count:] if from_end else non_summary_idx[:count])
    removed = [t for i, t in enumerate(items) if i in take]
    kept = [t for i, t in enumerate(items) if i not in take]
    return removed, kept


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


# #3783 stage 2's same-cause consecutive-recover cap (T3) is RETIRED
# (#5531 §10, settled (a), 2026-08-30) — see retry_loop's own "Bounded
# termination proof" docstring for why: once #5543's classify_llm_failure
# keeps Fatal/Retryable exceptions out of this ladder entirely, the ONLY
# thing that can recur here is a genuine, shrinkable Overflow — the
# halving ladder's own two floors already are the terminal condition,
# so a cap layered on top of them could only fire EARLIER, cutting the
# search off with real headroom still remaining. ``_last_recover_cause``/
# ``_consecutive_same_cause`` (retry_loop's own locals) are kept as pure
# telemetry on the ``compaction_shrink_recovered`` audit-event — nothing
# reads them to decide anything any more.


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


@dataclass(frozen=True)
class RetryPayload:
    """#5631: the single output of ``decompose_history_for_retry``, carried as
    one value.

    Not a bag assembled to shorten a signature — these five fields are produced
    together by one call and consumed together by one ladder, and ``seq_by_id``
    is only meaningful alongside the very turns it indexes (it maps
    ``id(wire_dict)`` to a real seq for exactly the dicts in ``head`` +
    ``raw_middle`` + ``tail``, #5498/#5578). Passing them flat costs 8
    parameters at the one call site, past the point #5631 sets for reaching
    for an object.

    Frozen because the ladder re-offers slices of these across attempts and
    must never be the thing that mutates them.

    Move Class (#5631 candidate 1, architect ruling — tui-coder's own
    finding on candidate 2): landed here, not a second, parallel Parameter
    Object in ``retry_loop``'s own signature — the two would represent
    the SAME data clump twice. Public name (was ``_RetryPayload`` in
    ``router_loop_driver.py``); that module now imports it from here,
    keeping the dependency direction runtime → services.
    """

    head: "list[dict]"
    raw_middle: "list[dict]"
    tail: "list[dict]"
    new_msg: dict
    seq_by_id: "dict[int, int]"


async def retry_loop(
    *,
    SP: str,
    payload: "RetryPayload",
    cfg: "CompactionConfig",
    model: str,
    engine: "CompactionEngine",
    learner: "TokenMultiplierLearner",
    main_call: Callable[..., Awaitable[Any]],
    spill_fn: "Callable[[list[dict]], list[tuple[int, dict]]] | None" = None,
    on_summary_used: "Callable[[ChatSummary, list[dict]], Awaitable[None]] | None" = None,
) -> Any:
    """Bounded shrink loop for context overflow recovery (PR-N6).

    #5631 candidate 1 (Fowler, Replace Function with Command — architect
    ruling, issue #5631 §1): this is now a thin entry point. The ladder's
    own 18 loop-carried locals live as :class:`RecoveryLadder` fields
    instead of function-local reassigned variables, and each rung /
    phase / floor is a named method instead of one 1,018-line body.
    ``head``/``raw_middle``/``tail``/``new_msg`` fold into ``payload``
    (:class:`RetryPayload`, reused from #5631 candidate 2 rather than a
    second Parameter Object for the same data clump) — ``model`` stays
    its own explicit param: it is the ROUTER purpose class
    (``ModelResolver.class_for_purpose("router")``), a DIFFERENT axis
    from ``engine.model`` (the compaction engine's own model, which per
    #3785 always follows ``Session.model`` directly and never the
    per-purpose map) — the two can genuinely diverge whenever an
    operator configures ``llm.model_class_by_purpose.router``, so
    deriving one from the other would be a real bug, not a simplification
    (verified before this refactor, per #5631 §1's own required
    precondition check; architect confirmed and revised the gate from
    "flat params ≤ 8" to "≤ 9" accordingly).

    Zero behavior change from before this refactor: same control flow,
    same branches, same events, same exceptions — see
    :class:`RecoveryLadder`'s own docstring for the full "Bounded
    termination proof," per-stage rationale, and parameter semantics,
    all relocated from here unchanged. Verified against the full
    behavior-invariance witness suite #5631 §1 names (#5592/#5612/#5296
    PR-2/#3783/PR-N6, all unchanged and green).
    """
    ladder = RecoveryLadder(
        SP=SP, payload=payload, cfg=cfg, model=model, engine=engine,
        learner=learner, main_call=main_call, spill_fn=spill_fn,
        on_summary_used=on_summary_used,
    )
    return await ladder.run()


# #5631 candidate 1: the sentinel `_run_one_iteration` returns wherever the
# former `retry_loop` body said a bare `continue` — a helper method cannot
# `continue` an outer loop directly, so `run()`'s own `while True:` reads
# this sentinel back to know "loop again" from "return this as the result."
# A module-level `object()` (not `None`/`False`) so it can never collide
# with a genuine recovered response value.
_LADDER_CONTINUE = object()


class RecoveryLadder:
    """The bounded shrink ladder for ONE context-overflow recovery episode
    (#5631 candidate 1 — Fowler's Replace Function with Command, applied
    to the former ``retry_loop`` module function).

    Constructed once per episode by :func:`retry_loop` (the public entry
    point, unchanged shape for the one production caller,
    ``router_loop_driver.py``); :meth:`run` is the former function's own
    ``while True:`` loop, now calling this class's own named stage
    methods instead of inlining ~1,000 lines of branching. Each rung /
    phase / floor keeps its OWN original comment, relocated verbatim —
    this is a STRUCTURAL commit only (Fowler: move ≠ edit); no branch,
    event, exception, or ordering changed. On success (normal path or
    after shrink), calls ``learner.observe`` with the actual vs
    estimated token count so the adaptive estimator learns.

    Bounded termination proof (#5531 §10 — ``max_iterations`` abolished)
    ----------------------------------------------------------------
    🔴 No iteration-count safety cap. Stopping is carried ENTIRELY by a
    lexicographic measure that strictly decreases on every path that
    does not return or raise: ``(T_max halvings remaining, total turn
    count, unspilled candidate count)`` — full proof (each component,
    both terminals, the episode-boundary reset, T3's retirement, scope):
    ``docs/deep-dives/decisions/0044-overflow-recovery-ladder.md#bounded-termination-proof-5531-10-max-iterations-abolished``
    (#5631 candidate 1: moved from here, Class A per the comment policy
    — history/measurement, not a decision this class itself makes).

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
    payload:
        :class:`RetryPayload` — ``head`` (HEAD turn list, oldest turns;
        may include one or more ``role==SUMMARY_MESSAGE_ROLE`` elements
        — #5531 PR-2: there is no separate ``summary`` parameter any
        more, removed; see ``_summary_tokens_in`` — a summary lives
        wherever it naturally sits in ``head``/``tail``, exactly like
        any other turn), ``raw_middle`` (middle turns not yet
        compacted), ``tail`` (TAIL turn list, most recent turns,
        verbatim), ``new_msg`` (incoming user message turn dict), and
        ``seq_by_id`` (opaque to this class — never read here; the
        CALLER's own ``id(wire_dict) -> seq`` map, threaded through
        unexamined for ``on_summary_used`` to resolve).
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
        args: SP, head, tail, new_msg.  Should raise
        ``ContextOverflowError`` on context-length error.
    on_summary_used:
        #5578/#5612 — optional async callable invoked once PER
        SUCCESSFUL ``compact()`` call this function makes (#5612: not
        deferred to this call's own eventual return — owner ruling, "a
        fold that already happened is durability-worthy the moment it
        happens", independent of whether the episode's own main_call
        later succeeds, fails, or this same call folds again), with that
        ``ChatSummary`` and the exact wire-dict turns it folded
        (``_offered`` at that specific compact() call's own site — never
        a cumulative union across more than one fold). ``None`` (the
        default) preserves this function's own pre-#5578 contract
        exactly: retry_loop stays a pure TRANSPORT operation with no
        persistence side effect of its own (#5498's own comment, this
        module) — persistence is the CALLER's decision, injected here
        the same way ``spill_fn`` is, never performed by this function's
        own body. NOT invoked for a call that never folds at all
        (recovered via spill/trim alone) — there is then no
        ``ChatSummary`` to report; MAY be invoked more than once for a
        single ``retry_loop`` call whose own raw_middle needed more than
        one fold pass. This function does NOT compute a real
        ``covers_through_seq`` for the reported turns — every wire dict
        here carries no ``seq`` (see ``SeqUnavailable.WIRE_DICTS_CARRY_
        NO_SEQ``'s own docstring); the caller is the one holding
        ``decompose_history_for_retry``'s own ``seq_by_id`` id()-keyed
        map and must derive the real value from it before persisting
        anything — trusting ``ChatSummary.covers_through_seq`` off this
        path would silently persist 0 (#5498's own load-bearing warning,
        re-confirmed by this change).
    """

    def __init__(
        self,
        *,
        SP: str,
        payload: "RetryPayload",
        cfg: "CompactionConfig",
        model: str,
        engine: "CompactionEngine",
        learner: "TokenMultiplierLearner",
        main_call: Callable[..., Awaitable[Any]],
        spill_fn: "Callable[[list[dict]], list[tuple[int, dict]]] | None" = None,
        on_summary_used: "Callable[[ChatSummary, list[dict]], Awaitable[None]] | None" = None,
    ) -> None:
        self._SP = SP
        self.head = payload.head
        self.raw_middle = payload.raw_middle
        self.tail = payload.tail
        self.new_msg = payload.new_msg
        self._cfg = cfg
        self._model = model
        self._engine = engine
        self._learner = learner
        self._main_call = main_call
        self._spill_fn = spill_fn
        self._on_summary_used = on_summary_used

        from reyn.llm.llm import note_upstream_recovery_call_attempt
        from reyn.llm.model_budget import get_max_input_tokens
        from reyn.runtime.services.token_multiplier_learner import detect_content_type

        self._note_upstream_recovery_call_attempt = note_upstream_recovery_call_attempt
        self._get_max_input_tokens = get_max_input_tokens
        self._detect_content_type = detect_content_type

        bg = engine.budgets
        self._bg = bg
        self._head_min_tokens = bg.head_budget
        self._tail_min_tokens = bg.tail_budget
        self._use_chars4 = cfg.use_chars4_estimate

        self._last_recover_cause: str | None = None
        self._consecutive_same_cause = 0

        self._init_recovery_scratch_state()

    def _init_recovery_scratch_state(self) -> None:
        """The recovery-ladder's own per-episode scratch fields -- split
        out of :meth:`__init__` (#5631 candidate 1, 150-line gate)
        purely structurally. Decision+reason (Class B) and relational
        (Class C) comments below stay inline, VERBATIM; the byte-limit
        reservation-redesign HISTORY (Class A) moved to
        ``docs/deep-dives/decisions/0044-overflow-recovery-ladder.md#byte-limit-reservation-redesign-4885-5531-pr-2``
        (#5631 candidate 1's own comment-policy pass, architect-
        authorized as part of this same PR rather than deferred)."""
        # #4885/#5531 PR-2: an HTTP 413 is a request-BODY-BYTE limit, a
        # different axis from the token budgets this ladder is built
        # from -- lowering the EFFECTIVE T_max is the only lever that
        # makes the EXISTING token-shrink mechanics respond to it (one
        # resource, one gate, not a second byte-built ladder). Binary
        # search: the byte/token ratio of whatever tripped a 413 is
        # unknown, so halving the SAME episode-scoped T_max override
        # converges in O(log T_max) steps regardless of the ratio. `SP`/
        # `new_msg`/the current summary are RESERVED -- fixed deductions
        # from the halved candidate, never apportioned by weight -- see
        # the doc above for the full owner-ruling history and the OLD
        # design this replaced.
        self._last_recover_is_byte_limit = False
        # #5316: the learned ceiling — read back (not re-measured, per issue
        # #5316's own "新しい測定は要りません") into the terminal message below
        # when this loop ends on a byte limit. ``None`` until this loop's own
        # accepted=True/False pair has been observed at least once each; a
        # turn whose FIRST attempt already 413s carries no accepted bound yet
        # (the terminal message degrades gracefully — see its own comment).
        self._last_accepted_wire_bytes: "int | None" = None
        self._last_rejected_wire_bytes: "int | None" = None
        self._t_max_override: "int | None" = None
        # #4947 ③ (architect-ruled): how many of ``raw_middle``'s turns the NEXT
        # ``compact()`` attempt should offer — ``None`` means "all of it" (the
        # normal, first-attempt case). Halved on each ``compact()`` failure,
        # reset to ``None`` on each success (a smaller *remainder* is then
        # attempted in full next time). This is the state that must actually
        # decrease for the split to terminate — see the shrink-escalation
        # comment below for why re-slicing the SAME ``raw_middle`` on every
        # iteration without persisting this would just recreate the old cycle.
        self._compact_attempt_len: "int | None" = None
        # #5631 candidate 1: the SENT slice for THIS iteration only --
        # computed once in ``_stage_fold`` and reused by ``_stage_spill``
        # (rung① offers exactly what rung②'s own compact() attempt just
        # sent, per #5592 — see ``_stage_spill``'s own docstring). Was a
        # plain local shared by both former inline sections of one
        # function body; now a field so the two extracted methods share
        # it without a param (there is no OTHER caller of either method,
        # so this is not a wider-scope leak).
        self._attempt_len: "int | None" = None
        # #4944①: tracks whether THIS iteration reached main_call -- reset
        # at the top of every _run_one_iteration call (an overflow from
        # compact() itself is a DIFFERENT, unmeasured payload -- guards
        # the failure-side wire_bytes emission so it never mislabels
        # "nothing resembling this was sent" as a rejected byte count).
        self._this_iteration_called_main_call = False
        # #5592 (owner ruling): the ORIGINAL raw_middle length this call
        # started with — captured once, never re-derived, so
        # ``compaction_shrink_recovered``'s own remaining-count field below is
        # always "N left out of the SAME original total" rather than a moving
        # denominator. This is the single producer for this count — #5588's
        # own ``levers_left`` (a separate, TUI-facing surface for the same
        # question) should read the SAME ``len(raw_middle)`` expression this
        # function already computes, not recompute it independently (lead-
        # coder/architect: "producer が1か所で数える、2か所で数えるとズレる").
        self._raw_middle_total = len(self.raw_middle)
        # SP/new_msg never shrink (see the floor comment above) and never
        # change across iterations (both are fixed parameters) — computed once,
        # not on every floor check.
        self._sp_tokens_floor = estimate_tokens(self._SP, self._model, use_chars4=self._use_chars4)
        self._new_msg_tokens_floor = estimate_tokens_for_turn(
            self.new_msg, self._model, use_chars4=self._use_chars4,
        )

    def _spill_batch_from_offered(self, offered: "list[dict]") -> int:
        """#5592 (owner ruling, superseding the withdrawn #5531 §10 "one
        candidate at a time" AND this PR's own withdrawn doubling-batch
        draft) — rung①: spill as many candidates from *offered* as
        ``spill_fn`` decides to hand back in ONE call, apply them all,
        return the count applied.

        ``offered`` — #5592 (owner ruling, correcting #9.6's own "never a
        slice" claim): the population is "the range THIS request is about
        to send" — ``raw_middle[:_attempt_len]`` at the call site below,
        which coincides with ``raw_middle`` entirely on the first attempt
        (``_compact_attempt_len is None``) and is the OFFERED SLICE only
        once rung② has halved at least once. #9.6's docstring text is
        stale as of this change; see the call site's own comment.

        ``spill_fn`` now returns ``list[tuple[int, dict]]`` (was
        ``tuple[int, dict] | None``) — a whole BATCH to apply this call,
        not one candidate. This function stays Spillability-agnostic
        either way (``spill_fn`` owns all tier/order/granularity
        decisions — this module never imports ``Spillability``, matching
        ``tool_result_cap.cap_tool_result_content``'s own ``save_fn``-
        injection style).

        #9.5's own "no cursor" rule still holds: every call re-scans the
        CURRENT ``offered`` fresh via ``spill_fn(offered)`` — no persisted
        position on this side either.

        Never reads wire bytes to decide progress (#5364 §1.6: a
        ``raw_middle`` spill cannot move wire bytes by construction,
        elided out of ``estimate_wire_bytes``; reading bytes here would
        discard every mid spill outright) — progress is "how many edits
        did ``spill_fn`` return," full stop."""
        if self._spill_fn is None or not offered:
            return 0
        edits = self._spill_fn(offered)
        for idx, replacement in edits:
            self.raw_middle[idx] = replacement
        return len(edits)

    def _stage_refill_phase1(self) -> bool:
        """ADR-0044 refill, Phase 1: if ``tail`` still holds non-summary
        content above ``_tail_min_tokens``, trim half of it (skipping any
        reserved summary element) into ``raw_middle`` and return ``True``.
        Returns ``False`` (no mutation) when the predicate does not hold —
        this fuses the former bare ``elif`` condition and its body into
        one call so :meth:`_run_one_iteration`'s own escalation chain AND
        :meth:`_stage_halve_room`'s own same-iteration re-check can share
        this exact mutation rather than duplicate it (#5631 candidate 1;
        the duplication itself pre-dates this PR — see the re-check's own
        comment, unchanged)."""
        if not (
            _estimate_tokens_list(self.tail, self._model, use_chars4=self._use_chars4) > self._tail_min_tokens
            and _has_non_summary(self.tail)
        ):
            return False
        # Phase 1: trim tail half → raw_middle. #5531 PR-2 (owner
        # ruling, issuecomment-5465590083): skip any summary element
        # (reserved — see _split_off_non_summary's own docstring)
        # rather than pulling from tail's raw front. The
        # `_has_non_summary` guard is load-bearing, not decorative:
        # once tail is ALL summary, the token check alone stays True
        # forever with zero progress possible — without this guard
        # Phase 1 "handles" the overflow every iteration while moving
        # nothing, starving the reservation ladder's own floor-check
        # (below) of ever being reached.
        _non_summary_tail_count = sum(
            1 for t in self.tail
            if not (isinstance(t, dict) and t.get("role") == SUMMARY_MESSAGE_ROLE)
        )
        chunk = max(_non_summary_tail_count // 2, 1)
        _removed, self.tail = _split_off_non_summary(self.tail, chunk, from_end=False)
        self.raw_middle.extend(_removed)
        return True

    def _stage_refill_phase2(self) -> bool:
        """ADR-0044 refill, Phase 2: same as :meth:`_stage_refill_phase1`
        for ``head`` (prepends into ``raw_middle`` and resets
        ``_compact_attempt_len`` — see the reset's own comment below)."""
        if not (
            _estimate_tokens_list(self.head, self._model, use_chars4=self._use_chars4) > self._head_min_tokens
            and _has_non_summary(self.head)
        ):
            return False
        # Phase 2: trim head half → raw_middle. #5531 PR-2: same skip
        # and same `_has_non_summary` guard as Phase 1's own branch.
        _non_summary_head_count = sum(
            1 for t in self.head
            if not (isinstance(t, dict) and t.get("role") == SUMMARY_MESSAGE_ROLE)
        )
        chunk = max(_non_summary_head_count // 2, 1)
        _removed, self.head = _split_off_non_summary(self.head, chunk, from_end=True)
        self.raw_middle = _removed + self.raw_middle
        # #5531 §10 (table #14's own asymmetry, corrected): Phase 2
        # PREPENDS to raw_middle — a stale ``_compact_attempt_len``
        # ("the first N turns failed") would now name a DIFFERENT
        # prefix than the one that fact was ever true of. Phase 1
        # (below-this-branch's sibling, APPENDS to the end) does NOT
        # reset — the prefix stays unchanged there, so the knowledge
        # stays valid.
        self._compact_attempt_len = None
        return True

    def _stage_spill(self) -> bool:
        """ADR-0044 rung① — spill. #5531 §10 (owner ruling, "spill is the
        ladder's first rung"): try spilling before touching
        ``_compact_attempt_len`` at all. Looping via ``_LADDER_CONTINUE``
        (was bare ``continue``) re-runs ``compact()`` on the SAME
        ``_attempt_len`` slice (unchanged by a spill — only the CONTENT
        of the spilled candidates shrank) — #9.5's own no-cursor rule:
        each call re-scans fresh, so this naturally keeps consuming
        candidates until either the overflow resolves or the population
        is exhausted.

        #5592 (owner ruling, superseding this PR's own withdrawn
        doubling-batch draft — real-machine incident: 2469 raw_middle
        candidates meant 2469 compact() calls at ~6s each, ~4.1 hours;
        see issue #5592 for the full incident trace): ``spill_fn`` now
        decides, in ONE call, how many candidates to hand back — this
        rung just applies whatever it returns and retries.
        ``chat.compaction.spill_granularity`` (the caller's own config,
        this function stays agnostic to it) controls ``spill_fn``'s own
        batch size: ``tier`` (default) returns every eligible candidate
        sharing the SAME ``Spillability`` tier in one shot (O(1) calls
        per overflow regardless of N); ``turn`` reproduces the
        pre-#5592 one-candidate-at-a-time behavior exactly (O(N) calls)
        — a config escape hatch, explicitly documented as NOT the safe
        default (``docs/reference/config/reyn-yaml.md``).

        #5592 (owner ruling, correcting #9.6): the population for THIS
        spill attempt is ``raw_middle[:_attempt_len]`` — the SAME slice
        this iteration's own ``compact()`` call just sent and had
        rejected, not ``raw_middle`` in its entirety. These coincide on
        the very first attempt (``_attempt_len == len(raw_middle)`` when
        ``_compact_attempt_len`` is still ``None``); they diverge only
        after rung② has halved at least once, and it is the SENT slice
        that must be offered to spill, not the untried remainder sitting
        past it.

        Returns whether any candidate was actually spilled — the caller
        (:meth:`_run_one_iteration`) returns :data:`_LADDER_CONTINUE`
        when it does (rung① exhausted, or no ``spill_fn`` at all, falls
        through to :meth:`_stage_halve_slice`)."""
        return self._spill_batch_from_offered(self.raw_middle[:self._attempt_len]) > 0

    def _stage_halve_slice(self) -> None:
        """ADR-0044 rung② — halve the slice. Only reached once rung①
        (:meth:`_stage_spill`) is exhausted (or no ``spill_fn`` at all).
        #5531 §10 (architect/owner correction, 2026-08-30): "fail →
        halve, succeed → double" — a genuine binary search, not the old
        one-way ratchet. A SUCCESS's own doubling happens at the success
        site itself (inside :meth:`_stage_fold`, right after
        ``raw_middle = raw_middle[_attempt_len:]`` — the only place that
        knows "this attempt just succeeded"), never here; this method
        only ever HALVES, because reaching it means the LAST attempt at
        the current ``_attempt_len`` just failed.

        Terminal (mid floor): raises :class:`UnrecoveredError` when a
        single turn offered alone still overflows AND spilling it (just
        tried by the caller) did not resolve it either — halving further
        cannot produce a smaller nonzero slice. #5531 §3 item 12:
        mode-independent (a floor is a floor regardless of which HTTP
        shape triggered the overflow)."""
        _current_attempt = (
            self._compact_attempt_len if self._compact_attempt_len is not None
            else len(self.raw_middle)
        )
        if _current_attempt <= 1:
            raise UnrecoveredError(
                (
                    "retry_loop: HTTP 413 (a request-BODY-BYTE limit) "
                    if self._last_recover_is_byte_limit
                    else "retry_loop: shrinking "
                ) + "recurred compacting a single raw_middle turn "
                "alone — mid cannot be split any further (the "
                "turn-count floor), and spilling every available "
                "candidate in raw_middle did not resolve this "
                "either." + (
                    _learned_byte_limit_clause(
                        last_accepted_wire_bytes=self._last_accepted_wire_bytes,
                        last_rejected_wire_bytes=self._last_rejected_wire_bytes,
                    ) if self._last_recover_is_byte_limit else ""
                ),
                terminal=RetryLoopTerminal.MID_FLOOR,
                saw_byte_limit=self._last_recover_is_byte_limit,
            )
        self._compact_attempt_len = max(_current_attempt // 2, 1)

    def _stage_halve_room(self) -> None:
        """ADR-0044 — halve the room. Reached once ``raw_middle`` is
        empty and neither :meth:`_stage_refill_phase1` nor
        :meth:`_stage_refill_phase2` had anything left to trim -- see
        this method's own inline comments for the reservation-redesign
        rationale, the room-floor terminal, and the same-iteration
        refill re-check."""
        _summary_tokens_current = _summary_tokens_in(
            self.head, self.tail, model=self._model, use_chars4=self._use_chars4,
        )
        _reserved = (
            self._sp_tokens_floor + self._new_msg_tokens_floor + _summary_tokens_current
        )
        _t_max_for_candidate = (
            self._t_max_override if self._t_max_override is not None
            else self._get_max_input_tokens(self._model)
        )
        _candidate = _t_max_for_candidate // 2
        if _candidate <= _reserved:
            raise UnrecoveredError(
                (
                    "retry_loop: HTTP 413 (a request-BODY-BYTE limit) "
                    if self._last_recover_is_byte_limit
                    else "retry_loop: shrinking "
                ) + "recurred even after binary-"
                f"search-halving the in-turn token ceiling to "
                f"{_candidate} tokens — SP ({self._sp_tokens_floor} "
                f"tokens), the newest message "
                f"({self._new_msg_tokens_floor} tokens), and the current "
                f"summary ({_summary_tokens_current} tokens) alone no "
                "longer fit, and none of the three is ever shrunk "
                "here. Even reducing head/tail to zero would not "
                "make this fit." + (
                    _learned_byte_limit_clause(
                        last_accepted_wire_bytes=self._last_accepted_wire_bytes,
                        last_rejected_wire_bytes=self._last_rejected_wire_bytes,
                    ) if self._last_recover_is_byte_limit else ""
                ),
                terminal=RetryLoopTerminal.ROOM_FLOOR,
                saw_byte_limit=self._last_recover_is_byte_limit,
            )
        self._t_max_override = _candidate
        _room = _candidate - _reserved
        # head/tail apportion `room` by their own component_weights
        # share, renormalised over just the two of them (body/new_msg
        # excluded — see this method's own opening docstring). A
        # missing/zero head+tail weight configuration falls back to
        # an even split rather than raising here — a config validity
        # question belongs to `assert_static_bounds` at startup, not
        # a mid-turn recovery path.
        _cw = self._cfg.component_weights
        _head_tail_weight = _cw.get("head", 0) + _cw.get("tail", 0)
        if _head_tail_weight > 0:
            self._head_min_tokens = int((_cw.get("head", 0) / _head_tail_weight) * _room)
            self._tail_min_tokens = _room - self._head_min_tokens
        else:
            self._head_min_tokens = _room // 2
            self._tail_min_tokens = _room - self._head_min_tokens
        # #5531 PR-2 (owner: "下限を割ったことが見える" — visible with
        # the shipped config, not just inferable from a shrunk wire):
        # this ladder just lowered the floor below what
        # `component_weights` configured (`bg` here is still the
        # UNCHANGED, entry-time budget — this method never reassigns
        # it) — emit that fact rather than let it pass silently
        # (previously true only of the byte-limit path; now true of
        # both).
        self._engine._events.emit(
            "compaction_floor_lowered",
            t_max_override=self._t_max_override,
            head_min_tokens=self._head_min_tokens,
            tail_min_tokens=self._tail_min_tokens,
            configured_head_budget=self._bg.head_budget,
            configured_tail_budget=self._bg.tail_budget,
            saw_byte_limit=self._last_recover_is_byte_limit,
        )
        # Immediately re-check tail/head against the NEW, smaller
        # minimums and shrink in this SAME iteration if either now
        # exceeds them — without this, halving the ceiling costs one
        # iteration and shrinking content down to it costs a second
        # (main_call retried with UNCHANGED content just re-confirms
        # the same overflow, wasting a turn of `max_iterations`),
        # roughly halving how many halvings fit under the safety cap
        # for no reason: the data needed to shrink (tail/head, already
        # read above) is already in hand at this exact point.
        if self._stage_refill_phase1():
            pass
        elif self._stage_refill_phase2():
            pass
        # If neither exceeds the new minimums either (head/tail were
        # ALREADY below even the halved ceiling), there is nothing left
        # to trim yet — still falls through without raising; the NEXT
        # overflow (same content, same call) halves the ceiling again
        # on its own next pass through this branch, continuing the
        # search.

    def _classify_and_wrap_compact_failure(self, exc: Exception) -> "None":
        """Classify an exception raised from ``compact()`` inside
        :meth:`_stage_fold` and either re-raise it bare (FATAL/
        RETRYABLE) or wrap it as :class:`CompactionOverflowError` for
        :meth:`_stage_fold`'s own ``except`` to catch (OVERFLOW). Always
        raises — never returns normally.

        #5543 / #5531 §10 (owner precondition for abolishing
        `max_iterations`, landed in this SAME PR — see this class's own
        "Bounded termination proof" docstring item 1): classify BEFORE
        deciding whether to enter the shrink ladder at all. Only
        OVERFLOW ever gets wrapped as `CompactionOverflowError` and
        offered to the ladder — the ladder's own termination proof
        depends on every exception that reaches it being genuinely
        shrinkable.

        FATAL (owner, #3783 verbatim): "An AttributeError in our own
        code must not become 'quietly shrink, then UnrecoveredError'" —
        re-raised bare, immediately, never shrunk. Without T3 (retired,
        #5531 §10) this is now the ONLY thing standing between a genuine
        reyn-side bug and the whole T_max floor being walked burning
        real LLM calls first.

        RETRYABLE (5xx/rate-limit/network/quota, including #5329's own
        quota-exhaustion incident this replaces — a provider usage-
        window exhaustion that used to get wrapped here and burn 2+
        wasted round-trips into the SAME dead window before T3 finally
        gave up): re-raised bare too — shrinking the input cannot fix an
        infra condition or a wait-bound quota window either. #5543's own
        spec routes RETRYABLE through the SAME backoff machinery the
        router already uses (`llm.py`'s `_llm_call_with_retry`) rather
        than here — compact()'s own LLM call is not yet wired into that
        machinery (a disclosed, separate gap: it has never been
        retried, #5543's own second named defect) — re-raising bare,
        unwired, is still SAFE (the SAME bare-propagation shape #5256's
        quota gate and `_handle_inbox_text`'s generic catch-all already
        handle without ending the session), just not yet backoff-
        retried; wiring that in is future scope, not a regression this
        PR introduces (quota's own bare re-raise, the ONE RETRYABLE
        member this site already special-cased, is UNCHANGED behavior).

        OVERFLOW (including the bare "matches none of FATAL/RETRYABLE"
        fallthrough): #3783 stage 3's own OWNER-RATIFIED default for
        THIS site specifically — wrapped and offered to the shrink
        ladder, bounded by the same-cause cap
        (test_input_independent_exception_hits_the_cap_not_an_infinite_
        loop). Deliberately still the bare ``classify_llm_failure``
        check, not the stronger ``is_shrinkable_overflow``.

        #5622 (issue) proposed unifying this site onto
        ``is_shrinkable_overflow`` too — the SAME stronger predicate
        #5593 introduced for ``router_loop_driver.py``'s own 2 arms.
        Tried, then reverted here: #5593's own PR body scopes that
        stronger default EXPLICITLY to "this module's [i.e.
        router_loop_driver.py's] own two call sites only" — because
        those 2 arms catch bare ``Exception`` from ``loop.run()``
        (literally anything), where an unclassifiable exception silently
        walking the whole shrink ladder is unbounded blast radius. THIS
        site only ever catches an exception from inside ``compact()``
        itself — the narrower surface #3783's owner ruling ("only
        exceptions that make compaction IMPOSSIBLE TO CONTINUE should
        propagate; the default should be recover") deliberately
        targeted, and #3783 stage 3's own ratified test still exercises
        today. Applying #5593's stronger check here silently inverted
        that owner ruling for this one site, regressing 11 pre-existing
        tests (#3783/#4947/#5630) under CI (lead-coder's own catch, PR
        #5642) — see that PR's own body for the full trace.

        The real, still-valid gain #5622 (issue) named is KEPT:
        ``is_shrinkable_overflow`` itself now lives here (public,
        importable) instead of being duplicated as a
        router_loop_driver.py-local function — router_loop_driver.py's
        own 2 sites import it from here unchanged. Only the 3rd site's
        OWN discriminator stays the bare ``classify_llm_failure`` check
        it always was — 2 deliberately different discriminators for 2
        deliberately different blast radii, not a drift."""
        if classify_llm_failure(exc) is not LLMFailureClass.OVERFLOW:
            raise
        raise CompactionOverflowError(str(exc)) from exc

    async def _stage_fold(self) -> "object | None":
        """ADR-0044 -- fold. Compacts ``raw_middle`` (or the first
        ``_attempt_len`` of it) into the running summary via
        ``compact()``. Returns :data:`_LADDER_CONTINUE` when a
        still-uncompacted remainder stays in ``raw_middle`` after a
        successful fold (the rest of THIS iteration is spent folding
        it, never handed to ``main_call`` incomplete); returns
        ``None`` (fall through to :meth:`_attempt_main_call`) when
        ``raw_middle`` was already empty, or the fold left nothing
        behind. An overflow from ``compact()`` itself is classified
        (FATAL/RETRYABLE re-raise bare; OVERFLOW wraps as
        `CompactionOverflowError` for the caller's own `except` to
        catch) -- see the classification's own inline comment for the
        full FATAL/RETRYABLE/OVERFLOW rationale."""
        if not self.raw_middle:
            return None
        # Compact raw_middle into the running summary.
        # Build section_token_caps from budgets.section_caps.
        section_caps = self._bg.section_caps if self._bg.section_caps else {
            "topic_arc": 200, "decisions": 400, "pending": 400,
            "session_user_facts": 200, "artifacts_referenced": 300,
        }
        # #4947 ③: offer only the first ``_compact_attempt_len``
        # turns when a prior attempt this call already failed —
        # ``None`` (no prior failure yet) offers all of it, the
        # same as before this change.
        self._attempt_len = (
            self._compact_attempt_len if self._compact_attempt_len is not None
            else len(self.raw_middle)
        )
        # #5531 (lead-coder ruling, issuecomment-5463182279): the
        # input to THIS compact() call is simply "the span being
        # folded right now" — `_offered`. If a summary element
        # already sits inside `raw_middle` (decompose's own turns
        # filter places it there whenever it doesn't fit within
        # head/tail — see `decompose_history_for_retry`), it rides
        # along and gets folded together with the rest, same as
        # any other turn; if it sits outside `raw_middle` (in
        # `head`/`tail` instead), it simply isn't part of this
        # fold. No line here decides WHICH — `_offered`'s own
        # existing chronological order already answers it. A
        # history can therefore end up holding more than one
        # `role=="summary"` turn (the untouched original still in
        # head/tail, plus a fresh one this fold produces) — see
        # `HistoryChunkToCompact`'s own docstring for the
        # consequence swept for callers assuming exactly one.
        _offered = self.raw_middle[:self._attempt_len]
        _messages = _offered
        input_chunk = HistoryChunkToCompact(
            messages=_messages,
            section_token_caps=section_caps,
        )
        try:
            return await self._apply_compact_call(input_chunk, _offered)
        except Exception as exc:
            self._classify_and_wrap_compact_failure(exc)
        return None

    async def _attempt_main_call(self) -> Any:
        """Sends the router's own main call with the CURRENT head/tail/
        new_msg (whichever summary element(s) belong on this wire
        already sit inside them -- #5531 PR-2, no separate ``summary=``
        param any more) and, on success, observes actual-vs-estimated
        tokens for the learner and emits the wire-bytes-measured
        telemetry event. Returns the response. Raises
        :class:`CompactionOverflowError`/:class:`ContextOverflowError`
        on overflow -- caught by :meth:`_run_one_iteration`'s own
        ``except``, which calls :meth:`_record_overflow_classification`."""
        # #5531 PR-2: `main_call` no longer takes a separate `summary=`
        # — whichever summary element(s) belong on this wire already
        # sit inside `head`/`tail` themselves.
        self._this_iteration_called_main_call = True
        # #5592: same single-producer mark as the compact() call site
        # above — main_call is retry_loop's OTHER upstream call.
        self._note_upstream_recovery_call_attempt()
        response = await self._main_call(
            SP=self._SP,
            head=self.head,
            tail=self.tail,
            new_msg=self.new_msg,
        )

        # Success: observe actual vs estimated tokens for the learner.
        content_type = self._detect_content_type(self.new_msg.get("content"))
        sp_tokens = estimate_tokens(self._SP, self._model, use_chars4=self._use_chars4)
        head_tokens = _estimate_tokens_list(self.head, self._model, use_chars4=self._use_chars4)
        tail_tokens = _estimate_tokens_list(self.tail, self._model, use_chars4=self._use_chars4)
        new_msg_tokens = estimate_tokens_for_turn(self.new_msg, self._model, use_chars4=self._use_chars4)
        # #5531 PR-2: no separate summary term here — head_tokens/
        # tail_tokens already include it (a summary element is just
        # one more entry in those lists now); adding it again would
        # double-count.
        estimate = sp_tokens + head_tokens + tail_tokens + new_msg_tokens

        actual: int | None = None
        try:
            usage = getattr(response, "usage", None)
            if usage is not None:
                actual = usage.prompt_tokens
        except Exception:
            pass

        if actual and estimate > 0:
            self._learner.observe(
                model=self._model,
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
        # #5531 PR-2: summary=None — head/tail already include any
        # summary element's own bytes; a separate value here would
        # double-count it in `.total`.
        _accepted_breakdown = estimate_wire_bytes_breakdown(
            SP=self._SP, head=self.head, summary=None, tail=self.tail, new_msg=self.new_msg,
        )
        self._last_accepted_wire_bytes = _accepted_breakdown.total
        self._engine._events.emit(
            "compaction_wire_bytes_measured",
            wire_bytes=_accepted_breakdown.total,
            accepted=True,
            sp_bytes=_accepted_breakdown.sp_bytes,
            head_bytes=_accepted_breakdown.head_bytes,
            summary_bytes=_accepted_breakdown.summary_bytes,
            tail_bytes=_accepted_breakdown.tail_bytes,
            new_msg_bytes=_accepted_breakdown.new_msg_bytes,
        )

        # #5612: on_summary_used is no longer called HERE — #5578's own
        # deferred-to-return call is superseded by the immediate,
        # per-fold call above (owner ruling: each successful fold is
        # durability-worthy the moment it happens, not only once the
        # whole episode's own main_call succeeds).
        return response

    def _record_overflow_classification(self, _overflow_exc: Exception) -> None:
        """Classify the overflow that reached :meth:`_run_one_iteration`'s
        own ``except`` (from either :meth:`_stage_fold` or
        :meth:`_attempt_main_call`), track the same-cause streak
        (telemetry only -- #5531 §10 retired the T3 cap this used to
        gate), and emit the ``compaction_wire_bytes_measured``
        (rejected) and ``compaction_shrink_recovered`` events. #3783
        stage 3: names the WRAPPED exception's own type, not the
        wrapper's -- every compact()-call failure is wrapped as
        ``CompactionOverflowError`` (see
        ``_classify_and_wrap_compact_failure``'s own raise site), so
        ``type(_overflow_exc).__name__`` would always read the same
        constant string regardless of what actually failed."""
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
        if _cause == self._last_recover_cause:
            self._consecutive_same_cause += 1
        else:
            self._last_recover_cause = _cause
            self._consecutive_same_cause = 1
        # #4885: same status_code check `is_context_overflow_error` uses
        # (a real attribute litellm/openai set from the underlying HTTP
        # response, not string-matched) — checked on the ROOT cause, the
        # same one `_cause` above already names, so "413" and the cause
        # name agree about what actually happened.
        self._last_recover_is_byte_limit = (
            getattr(_overflow_exc.__cause__, "status_code", None) == 413
        )
        if self._last_recover_is_byte_limit and self._this_iteration_called_main_call:
            # #4944①: the size that WAS SENT and got REJECTED — the
            # real limit is < this value (an upper bound), the pair to
            # the ``accepted=True`` emission above. ``head``/``tail``/
            # ``new_msg`` still hold exactly what this failed attempt
            # sent — the shrink-escalation ladder below has not run
            # yet this iteration. Guarded on
            # ``_this_iteration_called_main_call``: a compact()-origin
            # 413 raised from a DIFFERENT, unmeasured payload (see the
            # flag's own comment above the loop).
            # #5316: same breakdown as the accepted=True site; feeds
            # the learned-limit read-back in the terminal message
            # below. #5531 PR-2: summary=None — head/tail already
            # include any summary element's own bytes; a separate
            # value here would double-count it in `.total`.
            _rejected_breakdown = estimate_wire_bytes_breakdown(
                SP=self._SP, head=self.head, summary=None, tail=self.tail, new_msg=self.new_msg,
            )
            self._last_rejected_wire_bytes = _rejected_breakdown.total
            self._engine._events.emit(
                "compaction_wire_bytes_measured",
                wire_bytes=_rejected_breakdown.total,
                accepted=False,
                sp_bytes=_rejected_breakdown.sp_bytes,
                head_bytes=_rejected_breakdown.head_bytes,
                summary_bytes=_rejected_breakdown.summary_bytes,
                tail_bytes=_rejected_breakdown.tail_bytes,
                new_msg_bytes=_rejected_breakdown.new_msg_bytes,
            )
        self._engine._events.emit(
            "compaction_shrink_recovered",
            cause=_cause,
            iteration=self._iteration,
            consecutive=self._consecutive_same_cause,
            t_max_override=self._t_max_override,
            # #5592 (owner ruling): the absolute remaining/original
            # candidate count ("5/2469" form, never a bar/percentage —
            # a percentage hides the magnitude that was exactly the
            # thing #5592's own incident could not see). Real, exact
            # counts (len() on the live list and its captured original
            # length) — not an estimate.
            raw_middle_remaining=len(self.raw_middle),
            raw_middle_total=self._raw_middle_total,
        )
        # #4885: this cap is skipped for a byte-limit cause. It exists to
        # catch a TOKEN-shrink that keeps recovering the SAME cause
        # without ever changing anything — evidence shrinking cannot fix
        # THAT cause.
        #
        # #5531 §10 (settled (a), 2026-08-30): T3 (the same-cause
        # cap) is RETIRED — "don't fire T3 until the halving ladder
        # is exhausted" turned out to mean T3 never had anything
        # left to catch: the halving ladder's own floors ((a) below
        # and (b), the T_max-candidate floor) ARE the terminal
        # condition once exhausted, so a cap layered on top of them
        # could only ever fire EARLIER, cutting the search off with
        # headroom still remaining (the #4947 ③ finding that first
        # exempted the byte-limit cause from this cap, generalised:
        # #5543's classify_llm_failure now keeps Fatal/Retryable
        # OUT of this ladder entirely, closing the "genuinely stuck
        # cause" case T3 used to exist to catch). ``_cause``/
        # ``_consecutive_same_cause`` stay as pure telemetry on the
        # ``compaction_shrink_recovered`` event below — no branch
        # reads them to decide anything any more.

    def _advance_state_after_fold(self, chat_summary: "ChatSummary") -> "object | None":
        """Applies a successful ``compact()`` result -- replaces any
        prior summary in ``head`` with the fresh one, advances
        ``raw_middle`` past the attempted slice (doubling
        ``_compact_attempt_len`` for the remainder), and resets the
        same-cause streak. Split out of :meth:`_apply_compact_call`
        purely structurally (#5631 candidate 1, 150-line gate) --
        every rationale comment below is relocated VERBATIM. Returns
        :data:`_LADDER_CONTINUE` when a remainder stays in
        ``raw_middle``, else ``None``."""
        # #5531 PR-2 (owner ruling, §3 item 3, issuecomment-
        # 5463249759, deferred from PR-1 which reverted the
        # same line): the fold's result goes where the folded
        # span itself sat — between `head` and `tail` — so it
        # lands at the END of `head`. Not a position
        # decision: `head` keeps at most one summary element
        # per fold (any prior one this call already carried
        # is the SAME span's stale representative, replaced
        # here — never two summaries accumulating from ONE
        # fold). This is also what makes the summary reach
        # `main_call`'s wire at all once raw_middle later
        # empties — main_call receives `head`/`tail`
        # directly, never `raw_middle`.
        #
        # PR-1 reverted this exact line because it broke
        # retry_loop's OLD termination proof (`head` is no
        # longer monotonically non-growing once a fold can
        # re-add an element to it — Phase 2 below could pull
        # the just-appended element right back into
        # raw_middle and re-fold it, an observed
        # oscillation). What makes this safe to reintroduce
        # is THIS PR's own Phase 1/2 change (below): they now
        # SKIP any `role=="summary"` element when choosing
        # what to pull (`_split_off_non_summary`) — Phase 2
        # structurally cannot pull the element this line just
        # appended back into raw_middle any more, which is
        # what the old oscillation depended on. (#5531 PR-3:
        # this is now ALSO covered by the lexicographic
        # measure this function's own docstring proves —
        # ``max_iterations`` no longer exists as a backstop.)
        self.head = [
            t for t in self.head if t.get("role") != SUMMARY_MESSAGE_ROLE
        ] + [wrap_summary_as_message(chat_summary.to_dict())]
        # Only the ATTEMPTED slice is compacted — a smaller
        # remainder (if any) stays in raw_middle for a later
        # iteration. #5531 §10 (architect/owner correction,
        # 2026-08-30): ``_compact_attempt_len`` now DOUBLES
        # here (capped at the new, shorter raw_middle's own
        # length) rather than staying at whatever size just
        # worked — a slice that succeeded at a SMALL size
        # after prior halvings means the turn(s) that made it
        # fail before are now GONE (folded into the summary
        # just appended to ``head``), so the remainder is
        # more likely to compact in fewer, larger attempts;
        # holding the small size would keep folding the
        # remainder one attempt at a time for no reason once
        # the dominant turn is gone (owner: "1 件で 入った
        # ∴ その turn が 支配的だった ── それが消えた今 残りは
        # まとめて入る可能性が高い"). This does not reopen
        # #4950's own "uniformly-hard-to-compact input
        # re-discovers the same halving from scratch"
        # finding — THAT measured a full reset to ``None``
        # (the whole remainder) on every success; doubling is
        # bounded (a uniformly-hard input immediately fails
        # at the doubled size and halves right back down —
        # #10's own docstring works through both input
        # shapes). Any slice that turns out too large for the
        # new, smaller raw_middle just clips naturally at the
        # slice below; no explicit clamping needed there.
        # #5631 candidate 1: self._attempt_len is Optional in its own
        # __init__ declaration (unset until the first _stage_fold call
        # this iteration), but this method is only ever reached FROM
        # _apply_compact_call, itself only reachable once _stage_fold's
        # own setup has assigned it -- never None here in practice.
        assert self._attempt_len is not None
        self.raw_middle = self.raw_middle[self._attempt_len:]
        if self.raw_middle:
            self._compact_attempt_len = min(
                self._attempt_len * 2, len(self.raw_middle),
            )
        # #4947 ③ (CI red on #4950, architect-ruled): reset the
        # same-cause streak here, and ONLY here — not on any
        # other escalation branch. The cap's own words below
        # ("evidence shrinking cannot fix THAT cause" / "without
        # shrinking ever changing anything") are already false
        # the moment ONE slice compacts: this is the one branch
        # where work is PERMANENTLY reduced (the compacted
        # turns are gone from raw_middle for good, absorbed
        # into the summary element now in ``head``) — every
        # OTHER escalation branch
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
        self._last_recover_cause = None
        self._consecutive_same_cause = 0
        if self.raw_middle:
            # #4947 ③: ``main_call`` never receives ``raw_middle``
            # directly (only ``head``/``tail``/``new_msg``) —
            # calling it now would silently drop this still-
            # uncompacted remainder from what the LLM
            # actually sees. Spend the rest of THIS
            # iteration's budget compacting the remainder
            # instead of calling main_call with an incomplete
            # summary.
            return _LADDER_CONTINUE
        return None

    async def _apply_compact_call(self, input_chunk: "HistoryChunkToCompact", _offered: "list[dict]") -> "object | None":
        """The actual ``compact()`` call and its success-path
        application, split out of :meth:`_stage_fold` so its own
        ``try`` block stays short. On success: persists the fold via
        ``on_summary_used``, replaces any prior summary in ``head``
        with the fresh one, advances ``raw_middle`` past the attempted
        slice (doubling ``_compact_attempt_len`` for the remainder --
        #5531 §10), and resets the same-cause streak (the one branch
        where work is PERMANENTLY reduced). Returns
        :data:`_LADDER_CONTINUE` when a remainder stays in
        ``raw_middle`` (spend the rest of this iteration folding it
        rather than handing ``main_call`` an incomplete summary), else
        ``None``. Raises whatever ``compact()``/``on_summary_used``
        raise -- :meth:`_stage_fold`'s own ``except`` classifies it."""
        # #5592: mark this attempt as the NEXT upstream call
        # within the current recovery episode, exact and
        # single-producer (see note_upstream_recovery_call_
        # attempt's own docstring) — a no-op when no episode
        # is active (e.g. this function's own test-only direct
        # callers).
        self._note_upstream_recovery_call_attempt()
        # #5475: raw_middle's turns are wire dicts (no `seq` —
        # see `SeqUnavailable.WIRE_DICTS_CARRY_NO_SEQ`'s own
        # docstring) — a real seq is not available to this caller.
        chat_summary = await self._engine.compact(
            input_chunk, covers_through=SeqUnavailable.WIRE_DICTS_CARRY_NO_SEQ,
        )
        # #5612 scope note: a "discard a fold that does not
        # shrink" rule was drafted here and REMOVED outright
        # (not deferred, no follow-up issue filed) — a literal
        # size-only comparison (summary wire bytes vs offered
        # turns' own wire bytes) broke 10 unrelated,
        # pre-existing tests whose small/single-turn fixtures
        # structurally can't beat a structured summary's own
        # JSON overhead. The rule's own PREMISE was proven
        # false, not merely its threshold: a persisted summary
        # re-enters the population of the NEXT fold, so its
        # framing overhead is absorbed there rather than fixed
        # forever — architect confirmed the same inequality
        # holds under the alternative "compare against the
        # whole head" population too. This PR's own regression
        # 1 is resolved WITHOUT this rule — see the
        # `_compacted()` broadening in
        # test_5296_pr2_byte_reduction_same_turn_retry.py.
        # #5612 (owner ruling, verbatim: "そもそも compact
        # 成功してるのに 次回 元に戻るは あり得ないでしょ？"):
        # report EACH successful fold immediately, right here —
        # not deferred until this whole retry_loop call's own
        # eventual success/failure. A fold that already
        # happened is durable-worthy on its own; whether the
        # LATER main_call this episode is working toward
        # succeeds, fails, or this episode folds AGAIN after
        # this (raw_middle still non-empty, `continue` below)
        # is a separate question this fold's own durability
        # does not depend on — #5578's own irreversibility
        # argument ("discarding a fold does not restore
        # reversibility, it only guarantees paying again")
        # applies identically whether or not main_call
        # eventually succeeds. See on_summary_used's own
        # docstring for the full contract.
        if self._on_summary_used is not None:
            await self._on_summary_used(chat_summary, _offered)
        return self._advance_state_after_fold(chat_summary)

    async def run(self) -> Any:
        """Drive :meth:`_run_one_iteration` — the ladder's own former
        ``while True:`` loop body (#5631 candidate 1), unchanged control
        flow, branching, events, and exceptions — until it returns
        something other than ``_LADDER_CONTINUE`` (the former bare
        ``continue`` statements' own new signal, since a helper method
        cannot ``continue`` an outer loop directly) or raises."""
        # 0-indexed, matching the pre-#5531-§10 ``for _iteration in
        # range(max_iterations)`` numbering exactly (existing telemetry
        # consumers read the first pass as iteration 0).
        self._iteration = -1
        while True:
            self._iteration += 1
            outcome = await self._run_one_iteration()
            if outcome is not _LADDER_CONTINUE:
                return outcome

    async def _run_one_iteration(self) -> Any:
        """One pass of the ladder's own former ``while True:`` body
        (#5631 candidate 1) — returns the recovered response on success,
        :data:`_LADDER_CONTINUE` where the former body said bare
        ``continue`` (both call sites unchanged), or falls off the end
        (also treated as continue) exactly where the former body did
        neither — i.e. fell through the whole shrink-escalation section
        without hitting any of its own ``continue``s, letting the outer
        ``while True:`` simply loop again. Raises exactly where the
        former body did (``UnrecoveredError``, or a re-raised FATAL/
        RETRYABLE exception)."""
        self._this_iteration_called_main_call = False
        try:
            outcome = await self._stage_fold()
            if outcome is not None:
                return outcome
            return await self._attempt_main_call()
        except (CompactionOverflowError, ContextOverflowError) as _overflow_exc:
            self._record_overflow_classification(_overflow_exc)

        # Shrink escalation: reduce context size monotonically.
        if self.raw_middle:
            # ADR-0044: rung① (spill) then rung② (halve_slice) -- see
            # each method's own docstring for the full rationale.
            if self._stage_spill():
                return _LADDER_CONTINUE
            self._stage_halve_slice()
        elif self._stage_refill_phase1():
            pass
        elif self._stage_refill_phase2():
            pass
        else:
            # ADR-0044 — halve the room; see _stage_halve_room's own
            # docstring for the full rationale (reservation redesign,
            # room-floor terminal, same-iteration refill re-check).
            self._stage_halve_room()
        # #5531 §10: no code follows the loop body any more — every path
        # through it either returns (success), raises UnrecoveredError
        # (terminal (a) or (b), both inside the loop body above), or
        # falls through to `run`'s own next pass (#5631: via the
        # `_LADDER_CONTINUE` sentinel below, since a helper method
        # cannot `continue` an outer loop directly — same fall-through
        # meaning as the pre-#5631 bare `continue` this replaces).
        # `max_iterations` used to bound this loop from the OUTSIDE and
        # its exhaustion message lived here; both are gone (this
        # class's own docstring "Bounded termination proof" section is
        # the replacement).
        return _LADDER_CONTINUE


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
