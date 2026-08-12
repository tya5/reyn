"""RouterHistoryBuffer — history slicing and SP assembly for Session.

Owns:

  - build_history              — slice history into OpenAI-style messages
  - decompose_history_for_retry — head/raw_middle/tail/summary/seq_by_id for retry_loop
  - build_system_prompt        — assemble the router system prompt string
  - _serialise_turn            — materialise one ChatMessage to a wire dict

Also owns the module-level helpers:

  - _is_force_close_consolidation
  - _materialise_path_ref_content
  - _read_pathref_image

history_fn dependency: a zero-arg callable that returns the raw history list
(all ChatMessages including summaries) — passed in production as
``Session._active_branch_history`` (#2360's WAL-rewind-visibility filter,
NOT a bare ``lambda: self.history``; the two differ after a rewind, and
#4387 Phase B ② made that filtered view able to shrink/reorder call-to-call
by extending ``self.history`` backward on demand — see
``_incremental_elide_total``'s own docstring for why that matters here).
"""
from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    pass


# #2287 follow-up: the tool_call ↔ tool_result pairing repair moved OUT of this per-segment builder
# to the single provider chokepoint (``reyn.llm.wire_format.repair_tool_call_pairing`` in
# ``recorded_acompletion``). Per-segment repair was pair-blind across the head/bridge/tail assembly:
# an intact pair split by the bridge was duplicate-synthesized. The chokepoint repair sees the FULL
# assembled wire list, so it is the correct single place for the guarantee.


def _is_force_close_consolidation(summary: Any) -> bool:
    """#1092 PR-F2a: True iff a ``summary`` turn is a force-close handoff
    consolidation — identified by the dedicated ``consolidation`` structured
    field (set by the F2b handoff). This is the GATE for the durable
    covers-respecting reset in RouterHistoryBuffer.build_history:
    when present, the slicer drops the covered raw head/tail and slices
    ``[consolidation] + post-consolidation turns``. Normal compaction summaries
    lack the field → the slicer keeps its head/tail+bridge behaviour unchanged
    (normal chat stays byte-identical)."""
    structured = (summary.meta or {}).get("structured") or {}
    return bool(structured.get("consolidation"))


def _read_pathref_image(path: str, media_store: Any) -> bytes | None:
    """Resolve a path-ref to raw image bytes (issue #383 PR-C).

    Two cases:
      - Path inside the MediaStore's image directory (= Reyn-owned,
        from a tool result): read via ``media_store.read_image``.
      - Path elsewhere (= user-attached via ``/image``): read directly
        from disk so user files don't need to be copied into the
        workspace.

    Returns None when the path can't be resolved (missing file,
    permission denied, etc.). Caller drops the block in that case so
    the LLM message stays valid.
    """
    from pathlib import Path as _Path

    # Try the MediaStore first (= validates inside-media_dir + reads).
    if media_store is not None:
        try:
            data_bytes, found = media_store.read_image(path)
            if found:
                return data_bytes
        except PermissionError:
            # Not inside media_dir — try direct disk read below.
            pass
    # Direct disk read for user-attached files. Resolve relative paths
    # against CWD (= the chat session's project root convention).
    p = _Path(path)
    if not p.is_absolute():
        p = _Path.cwd() / p
    p = p.resolve()
    if not p.exists() or not p.is_file():
        return None
    try:
        return p.read_bytes()
    except OSError:
        return None


def _materialise_path_ref_content(
    content: str | list[dict], media_store: Any,
) -> str | list[dict]:
    """Issue #383 PR-C: convert path-ref content parts to inline data URLs
    at the LLM wire boundary.

    Three input cases:
      - str content → returned unchanged.
      - list content with no path-ref parts → returned unchanged.
      - list content with path-ref parts (= ``{"type":"image","path":...}``)
        → each path-ref is resolved via ``media_store.read_image`` and
        emitted as ``{"type":"image_url","image_url":{"url":"data:..."}}``.

    When ``media_store`` is None OR the path resolves outside the storage
    root OR the file no longer exists, the block is dropped (= conversation
    continues without it, no crash). Already-inline image_url parts pass
    through.
    """
    if isinstance(content, str) or not isinstance(content, list):
        return content
    has_pathref = any(
        isinstance(p, dict) and p.get("type") == "image" and p.get("path")
        for p in content
    )
    if not has_pathref:
        return content
    materialised: list[dict] = []
    for part in content:
        if not isinstance(part, dict):
            materialised.append(part)
            continue
        if part.get("type") != "image" or not part.get("path"):
            materialised.append(part)
            continue
        path = part["path"]
        mime = part.get("mime_type") or part.get("mimeType") or "image/png"
        data_bytes = _read_pathref_image(path, media_store)
        if data_bytes is None:
            continue
        import base64
        data_b64 = base64.b64encode(data_bytes).decode("ascii")
        materialised.append({
            "type": "image_url",
            "image_url": {"url": f"data:{mime};base64,{data_b64}"},
        })
    return materialised


def _refresh_skill_location_tokens(
    content: "str | list[dict]", meta: Any, project_dir_fn: "Callable[[], Any] | None",
) -> "str | list[dict]":
    """#3629: re-expand ``${REYN_SKILL_DIR}``/``${REYN_PLUGIN_ROOT}`` (+
    ``CLAUDE_*`` aliases) in a persisted ``load_skill`` tool-result entry,
    against the CURRENT filesystem, every time this turn's history is
    serialised for the wire — the "dynamic param" discipline
    (``reyn.plugins.tokens``'s ``REYN_PROJECT_DIR`` classification)
    extended to the two location tokens that used to get baked to an
    absolute value once and persisted forever.

    No-op (content returned unchanged) unless ALL of:
    ``content`` is a ``str`` (a tool message never uses the multimodal
    list-of-parts shape — mirrors :func:`_materialise_path_ref_content`'s
    own type gate), ``meta`` carries ``SKILL_SOURCE_PATH_META_KEY`` (set
    only by ``router_loop.py``'s tool-result assembly when
    ``load_skill_to_canonical`` supplied ``history_text``/``history_meta``
    — see ``chat_message.py``'s key docstrings), and *project_dir_fn* is
    not ``None`` (a legacy/test double with no workspace access degrades to
    "never refresh", same fail-closed idiom ``_materialise_path_ref_content``
    uses for a ``None`` ``media_store``).

    Pre-#3629 history has no ``SKILL_SOURCE_PATH_META_KEY`` at all — this
    function never touches it, matching the architect's ruling that
    already-poisoned entries are neither rewritten nor annotated.
    """
    if not isinstance(content, str) or not isinstance(meta, dict) or project_dir_fn is None:
        return content
    from reyn.runtime.chat_message import SKILL_SOURCE_PATH_META_KEY
    skill_source_path = meta.get(SKILL_SOURCE_PATH_META_KEY)
    if not skill_source_path:
        return content
    project_dir = project_dir_fn()
    if project_dir is None:
        return content
    from reyn.plugins.skill_load import refresh_location_tokens
    return refresh_location_tokens(
        content, skill_source_path=skill_source_path, project_dir=project_dir,
        alias_claude=True,
    )


# #4381 PR-1: warn-once cache for the resource/budget invariant below, keyed
# per (model, phase) — a high-frequency-called SSoT (every trigger
# resolution) must not warn on every call; process-global by design (mirrors
# `reyn.llm.litellm_bootstrap`'s own one-shot flags). Reset in tests via
# `_resource_budget_warned.clear()`.
_resource_budget_warned: "set[tuple[str, str | None]]" = set()


def _check_resource_within_budget(
    model: str, phase: "str | None", effective_trigger: int, events: Any,
) -> None:
    """#4381 PR-1: the invariant this SSoT now enforces — "a result that
    passed the resource boundary must not exceed the budget boundary after
    conversion" (architect design, #4381). Two independent read-cap-vs-
    spill-cap mismatches (#4381's own reported incident, #4432's spill-loop
    guard) trace to this SAME class going unchecked: the resource boundary
    (``control_ir_inline_cap``, CHARS — memory/transfer-scoped, model-
    derived today; owner ruling may make it a model-independent config byte
    value later, at which point THIS function's arguments alone can no
    longer derive it and a config value must be threaded in — not done in
    PR-1) and the budget boundary (``effective_trigger``, TOKENS — the
    model's context window) are never compared anywhere before this PR, so
    a resource-bounded result that looks "safe" in chars can still overflow
    the budget boundary once converted.

    The conversion point is ``context_builder.INLINE_CAP_CHARS_PER_TOKEN``
    — the ONE named chars→tokens conversion (architect: name it once, don't
    re-derive the same ratio elsewhere). Rounded UP (ceil): understating the
    resource bound's token cost would make this check too permissive, the
    wrong direction for a safety check.

    Warn-once per ``(model, phase)`` (not per call — this SSoT is called on
    every trigger resolution, so warning every time would flood the log)
    via a ``resource_cap_exceeds_budget_trigger`` audit-event. Detection
    only in PR-1 — no value is clamped; the class closes once the resource
    boundary itself moves to a model-independent byte value (later PR).
    """
    from reyn.core.context_builder import (
        INLINE_CAP_CHARS_PER_TOKEN,
        control_ir_inline_cap,
    )

    resource_bound_chars = control_ir_inline_cap(model, events=events, phase=phase)
    resource_bound_tokens = -(-resource_bound_chars // INLINE_CAP_CHARS_PER_TOKEN)  # ceil
    if resource_bound_tokens <= effective_trigger:
        return
    key = (model, phase)
    if key in _resource_budget_warned:
        return
    _resource_budget_warned.add(key)
    if events is not None:
        events.emit(
            "resource_cap_exceeds_budget_trigger",
            model=model, phase=phase or "",
            resource_bound_chars=resource_bound_chars,
            resource_bound_tokens=resource_bound_tokens,
            effective_trigger=effective_trigger,
        )


def resolve_effective_trigger_and_budgets(
    compaction_controller: Any, model: str, events: Any, *, phase: "str | None" = None,
) -> "tuple[int, int, int]":
    """Return ``(effective_trigger, head_budget, tail_budget)`` — #2957 PR-B
    single SSoT for this lookup.

    Before PR-B, :class:`RouterHistoryBuffer` (``_resolve_budgets``) and
    :class:`~reyn.runtime.services.context_budget_advisor.ContextBudgetAdvisor`
    (``_get_effective_trigger``) each reimplemented the identical
    ``compaction_controller._engine.budgets`` lookup + ``get_max_input_tokens``
    fallback independently — a duplication that could silently drift (one
    site's fallback changing without the other). Both now delegate here.

    #4381 PR-1: also the SSoT for the resource/budget invariant (see
    ``_check_resource_within_budget``) — the third instance of this same
    "two independently-computed things can silently drift" class this
    function already exists to close (PR-B's own docstring names the first
    two). ``phase`` (#4381: granularity is per ``(model, phase)``, not once
    per session — ``control_ir_inline_cap`` itself already takes ``phase``)
    defaults to ``None`` for both existing callers, neither of which has a
    phase concept today; a future phase-aware caller can pass a real value
    without a signature change.
    """
    engine = getattr(compaction_controller, "_engine", None) if compaction_controller is not None else None
    budgets = getattr(engine, "budgets", None)
    if budgets is not None:
        effective_trigger, head_budget, tail_budget = (
            budgets.effective_trigger, budgets.head_budget, budgets.tail_budget,
        )
    else:
        from reyn.llm.model_budget import get_max_input_tokens
        effective_trigger = get_max_input_tokens(model, events=events)
        fallback = effective_trigger // 4
        head_budget, tail_budget = fallback, fallback
    _check_resource_within_budget(model, phase, effective_trigger, events)
    return effective_trigger, head_budget, tail_budget


# ── RouterHistoryBuffer ───────────────────────────────────────────────────────


class RouterHistoryBuffer:
    """Router-view history slicer and system-prompt assembler for Session.

    Constructed once per Session; owns the three methods that build the
    context presented to the router LLM each turn.
    """

    def __init__(
        self,
        *,
        history_fn: Callable[[], list],   # zero-arg → raw history (all roles)
        compaction: Any,                  # CompactionConfig — use_chars4_estimate
        compaction_controller: Any,       # for engine.budgets
        model_fn: Callable[[], str],      # zero-arg → CURRENT resolved model (#1752)
        events: Any,                      # EventLog — for fallback tokens
        media_store: Any,                 # MediaStore | None — for _serialise_turn
        router_host: Any,                 # RouterHostAdapter — for build_system_prompt
        action_retrieval: Any,            # ActionRetrievalConfig — .universal_wrappers_enabled
        non_interactive: bool,
        reasoning: Any = None,            # ReasoningConfig — .continuity / .recent_turns (#1652/②)
        project_dir_fn: "Callable[[], Any] | None" = None,  # #3629: zero-arg → CURRENT workspace base_dir
    ) -> None:
        self._history_fn = history_fn
        self._compaction = compaction
        self._compaction_controller = compaction_controller
        self._model_fn = model_fn
        self._events = events
        self._media_store = media_store
        self._router_host = router_host
        self._action_retrieval = action_retrieval
        self._non_interactive = non_interactive
        # #1652/②: cross-turn reasoning rides the wire assistant messages
        # (native re-attach) instead of a router-SP text section. ReasoningConfig
        # gates it (.continuity) and bounds it (.recent_turns). None → off.
        self._reasoning = reasoning
        # #3629: live, not cached — the same "resolve at the moment it's needed"
        # discipline as ``_model`` above (a rewind/checkout/reinstall between
        # turns must be reflected, never the construction-time workspace).
        # ``None`` (a legacy/test double with no workspace access) degrades to
        # "never refresh location tokens" — _serialise_turn's own None-check.
        self._project_dir_fn = project_dir_fn
        # #4403: incremental elide-total cache. build_history() used to
        # re-estimate EVERY turn's token count on EVERY call just to check
        # "total <= effective_trigger" — O(session length) per turn, forever
        # (compaction shrinks what's SENT to the LLM, never self.history).
        # Measured (real litellm tokenizer, the config default): 5.13ms/turn
        # -> 559s at 108,896 turns (#4403). ``_TOKEN_CACHE_MAXSIZE=8192``'s
        # per-(model,text) cache cannot help here regardless of size: this
        # loop visits turns in the SAME order every call, so an 8192-entry
        # FIFO always evicts the early turns before a 100k-turn pass reaches
        # them again next call — 100% miss, not a tuning problem.
        # See ``_incremental_elide_total``'s own docstring for the
        # invalidation contract these three fields exist to support.
        self._cached_elide_total: int = 0
        self._cached_elide_turn_count: int = 0
        self._cached_elide_last_seq: "int | None" = None
        self._cached_elide_model: "str | None" = None
        self._cached_elide_use_chars4: "bool | None" = None

    @property
    def _model(self) -> str:
        # #1752: resolve the model live each call so a /model override (which can
        # change the context window) is reflected in token counting / trimming.
        # The session-side fn resolves the class → litellm string; without this
        # the buffer would count against the construction-time model.
        return self._model_fn()

    # ── Internal helpers ─────────────────────────────────────────────────────

    def _latest_summary(self, history: "list | None" = None) -> Any | None:
        """Return the most recent summary message, or None.

        ``history`` — the already-materialised history to search. #2939: callers
        that have ALREADY called ``self._history_fn()`` must pass it, because
        ``_history_fn`` is not a cheap accessor: in production it is
        ``Session._active_branch_history``, a recomputed rewind-aware view over
        the whole conversation. Re-invoking it here made one ``build_history``
        produce the view 2x (3x on the elide path, which calls this again) —
        multiplying the most expensive thing on the turn's hot path by 2-3.
        Omit it only where no history has been fetched yet (the fn is then
        invoked once, as before).
        """
        for m in reversed(self._history_fn() if history is None else history):
            if m.role == "summary":
                return m
        return None

    def _serialise_turn(self, m: Any) -> dict:
        """Serialise one ChatMessage into a litellm-compatible wire dict.

        #2957 PR-B: this method's output is the CANONICAL quantity for token
        accounting — it is what actually reaches the provider. Both the
        elide-threshold check in :meth:`build_history` /
        :meth:`decompose_history_for_retry` and
        :class:`~reyn.runtime.services.context_budget_advisor.ContextBudgetAdvisor`
        (which measures ``build_history``'s own returned wire dicts) now
        estimate tokens over THIS output, closing a prior circularity where
        the elide side measured serialise-INPUT (raw ChatMessage, pre-image-
        materialisation) while the advisor measured serialise-OUTPUT (the
        elided wire dicts) — two different quantities for the same
        conversation. Do not reintroduce a second "what does the provider
        see" quantity; measure this method's return value.

        Path-ref content parts (= ``{"type":"image","path":...}``) are
        materialised to data URLs at this boundary so storage stays light
        and the LLM sees the inline form it expects. Shared by
        :meth:`build_history` and :meth:`decompose_history_for_retry` so both
        produce identical wire shapes (the retry_loop decomposition must
        rebuild the same prompt the normal path would have sent).
        """
        # Legacy "agent" stragglers (= migrated entries that somehow bypassed
        # _migrate_legacy_chat_message) → normalise on read.
        role = "assistant" if m.role == "agent" else m.role
        content = _materialise_path_ref_content(m.content, self._media_store)
        # #3629: fresh, every serialise — re-resolve any location token a
        # persisted `load_skill` entry left literal, against the CURRENT
        # filesystem (see _refresh_skill_location_tokens's docstring).
        content = _refresh_skill_location_tokens(
            content, getattr(m, "meta", None), self._project_dir_fn,
        )
        msg: dict = {"role": role, "content": content}
        if m.tool_calls is not None:
            msg["tool_calls"] = m.tool_calls
        if m.tool_call_id is not None:
            msg["tool_call_id"] = m.tool_call_id
        if m.name is not None:
            msg["name"] = m.name
        # #1652/②: re-attach this assistant turn's captured reasoning natively
        # (reasoning_content / thinking_blocks) so litellm carries the model's
        # prior reasoning across turns — replacing the router-SP text section.
        # Gated by continuity; no-op (byte-identical) when reasoning is empty.
        # build_history applies the recent_turns bound after serialisation.
        if (
            role == "assistant"
            and self._reasoning is not None
            and getattr(self._reasoning, "continuity", False)
            and isinstance(getattr(m, "meta", None), dict)
        ):
            from reyn.runtime.reasoning_continuity import attach_reasoning
            attach_reasoning(msg, m.meta.get("reasoning"))
        return msg

    def _bound_wire_reasoning(self, messages: list[dict]) -> list[dict]:
        """#1652/②: bound native reasoning to the most recent ``recent_turns``
        assistant messages that carry it — mirrors the old text-section bound
        (gemini accumulates + bills reasoning in full unless bounded). Strips the
        reasoning fields from older assistant messages in-place. ``recent_turns
        <= 0`` (UNBOUNDED) keeps all. No-op when continuity is off / unconfigured.
        Returns ``messages`` for call-site chaining."""
        from reyn.runtime.reasoning_continuity import _REASONING_BUNDLE_FIELDS
        keep = getattr(self._reasoning, "recent_turns", 0) if self._reasoning else 0
        if keep <= 0:
            return messages
        carriers = [
            i for i, mm in enumerate(messages)
            if mm.get("role") == "assistant"
            and any(f in mm for f in _REASONING_BUNDLE_FIELDS)
        ]
        for i in carriers[:-keep]:
            for f in _REASONING_BUNDLE_FIELDS:
                messages[i].pop(f, None)
        return messages

    def _resolve_budgets(self) -> tuple[int, int, int]:
        """Return (effective_trigger, head_budget, tail_budget).

        #2957 PR-B: delegates to the module-level
        ``resolve_effective_trigger_and_budgets`` — single SSoT shared with
        ``ContextBudgetAdvisor._get_effective_trigger`` (previously each
        reimplemented this lookup independently).
        """
        return resolve_effective_trigger_and_budgets(
            self._compaction_controller, self._model, self._events,
        )

    def _incremental_elide_total(
        self, turns: list, wire_turns: list[dict], *, use_chars4: bool,
    ) -> int:
        """#4403: the ``total`` build_history needs for its "total <=
        effective_trigger" elide decision, computed incrementally instead
        of re-estimating every turn every call.

        ``turns`` (``ChatMessage`` list, pre-serialise) is what
        ``self._history_fn()`` (``Session._active_branch_history``)
        returned THIS call — append-only in the common case, but NOT
        guaranteed monotonic: a rewind/branch-switch can make it shorter
        or reorder it (``_active_branch_history`` re-derives WAL-branch
        visibility fresh every call). The cache's validity check is
        therefore structural, not a bare length comparison:

          - ``len(turns) < cached_turn_count`` -> definitely shrank
            (rewind past the cached boundary) -> recompute from scratch.
          - the ``seq`` at the cached boundary position no longer matches
            what was cached -> the prefix itself changed (branch-switch
            landing on a same-length-but-different list) -> recompute.
          - model or use_chars4 changed since the cache was built -> the
            cached per-turn costs were measured against a different
            tokenizer -> recompute (an O(1) check, not a reason to distrust
            the whole mechanism).
          - otherwise -> the cached prefix is still exactly what it was:
            add only the cost of the turns appended since, an O(k) pass
            where k is genuinely new turns, not O(session length).

        Every fallback path recomputes correctly (just not cheaply) — this
        is a performance cache, not a source of truth: a wrong invalidation
        guess costs CPU, never correctness, because the ``else`` branch
        always re-derives ``total`` from the real ``wire_turns`` this call
        actually has.
        """
        from reyn.services.compaction.engine import estimate_tokens_for_any_turn

        model = self._model
        cache_valid = (
            len(turns) >= self._cached_elide_turn_count
            and self._cached_elide_model == model
            and self._cached_elide_use_chars4 == use_chars4
            and (
                self._cached_elide_turn_count == 0
                or (
                    turns[self._cached_elide_turn_count - 1].seq
                    == self._cached_elide_last_seq
                )
            )
        )
        if cache_valid:
            new_wire_turns = wire_turns[self._cached_elide_turn_count:]
            total = self._cached_elide_total + sum(
                estimate_tokens_for_any_turn(wt, model, use_chars4=use_chars4)
                for wt in new_wire_turns
            )
        else:
            total = sum(
                estimate_tokens_for_any_turn(wt, model, use_chars4=use_chars4)
                for wt in wire_turns
            )

        self._cached_elide_total = total
        self._cached_elide_turn_count = len(turns)
        self._cached_elide_last_seq = turns[-1].seq if turns else None
        self._cached_elide_model = model
        self._cached_elide_use_chars4 = use_chars4
        return total

    # ── Public API ────────────────────────────────────────────────────────────

    def build_history(self) -> list[dict]:
        """Slice history into OpenAI-style messages for RouterLoop.

        #1128 step 3 (Fork B — window-utilization-first): the elide point now
        coincides with ``effective_trigger`` (the existing pre-frame compaction
        trigger) instead of the old turn-count head_size/tail_size.

        - If total token estimate <= effective_trigger: return ALL turns raw
          (no elide, no duplication).  The LLM sees the full conversation up
          to the compaction trigger.
        - Else: elide the middle — head (trim_head) + optional summary bridge
          + tail (trim_tail).  The pre-frame guard
          ``maybe_force_compact`` has already compacted the middle
          before this runs, so the elide point is structurally aligned.

        Overlap guard: if trim_head and trim_tail collectively cover all turns
        (the chat is small relative to budgets but total > trigger — unlikely
        but possible with large single turns), deduplication by identity
        ensures no turn appears twice.

        Returns [{role: 'user'|'assistant', content: str}, ...] ordered
        chronologically. The system prompt is prepended by RouterLoop itself.
        Only user/agent conversational turns are included; ``summary``
        remains Reyn-internal and is filtered out.
        """
        from reyn.services.compaction.engine import trim_head, trim_tail

        history = self._history_fn()
        # E-full (#383): include tool-turn entries (= assistant w/ tool_calls,
        # tool responses) in the slice. The wire-shape builder below
        # forwards them as-is to the LLM. ``summary`` remains
        # Reyn-internal and filtered out.
        turns = [
            m for m in history
            if m.role in ("user", "assistant", "tool", "agent")
        ]

        # #1092 PR-F2a: durable force-close reset. When the latest summary is a
        # force-close handoff consolidation (covers-all), the conversation
        # overflowed even when shrunk to its floor — so the slicer DROPS the
        # covered raw head/tail permanently and slices [consolidation bridge] +
        # the turns appended AFTER the consolidation. This is DURABLE (re-applied
        # every turn, not a one-shot override): the next user turn slices
        # [consolidation] + recent turns, never re-slicing the dropped raw
        # head/tail → no immediate re-overflow. Position-based (turns after the
        # consolidation in history order), NOT seq>covers — #3704 gave every
        # role a monotonic seq at persist time, but history predating that fix
        # still has assistant/tool entries stuck at seq==0 forever (no
        # backfill), so a seq filter would wrongly drop their post-handoff
        # replies on any session with pre-fix history. Position-based sidesteps
        # the old/new-history split entirely. GATED to force-close
        # consolidations only (the dedicated `consolidation` field) — normal
        # compaction summaries fall through to the unchanged head/tail+bridge
        # path below, so normal chat stays byte-identical.
        _fc_summary = self._latest_summary(history)
        if _fc_summary is not None and _is_force_close_consolidation(_fc_summary):
            from reyn.runtime.chat_message import ChatMessage
            _idx = next(
                (i for i, m in enumerate(history) if m is _fc_summary), -1
            )
            _post = [
                m for m in history[_idx + 1:]
                if m.role in ("user", "assistant", "tool", "agent")
            ]
            _summary_text = (
                _fc_summary.content if isinstance(_fc_summary.content, str)
                else json.dumps(_fc_summary.content, ensure_ascii=False)
            )
            _bridge = [ChatMessage(
                role="assistant",
                content=f"[summary of earlier conversation]\n{_summary_text}",
                ts=_fc_summary.ts,
            )]
            return self._bound_wire_reasoning(
                [self._serialise_turn(m) for m in (_bridge + _post)]
            )

        effective_trigger, head_budget, tail_budget = self._resolve_budgets()
        use_chars4 = getattr(self._compaction, "use_chars4_estimate", False)

        # #2957 PR-B: serialise ALL candidate turns to their wire-dict shape
        # UP FRONT, then measure/trim/select on THAT — the canonical quantity
        # (see ``_serialise_turn``'s docstring). Before PR-B this elide-
        # threshold total summed the pre-serialise ChatMessage instances
        # (via ``estimate_tokens_for_any_turn``'s ChatMessage-adapting
        # branch), while ContextBudgetAdvisor measured this method's
        # returned (post-serialise) wire dicts — two different quantities
        # for the same conversation. Both now go through
        # ``estimate_tokens_for_any_turn`` on the SAME wire dicts (the dict
        # branch is still needed here, not a direct
        # ``estimate_tokens_for_turn`` call — a wire dict's ``tool_calls``
        # is a separate top-level key, see that function's docstring).
        # Serialising once here and reusing the result for both the
        # total-check AND the final return also avoids a double
        # ``_serialise_turn`` call on the surviving subset.
        #
        # #3185 (MEASURED, closed won't-fix — do NOT "optimise" this back into
        # a lazy/partial serialise): serialising every CANDIDATE (not just the
        # survivors) means an image-bearing turn is base64-materialised even
        # when it is about to be elided away. Re-measured against the six
        # largest real ``history.jsonl`` conversations available (up to 2969
        # turns): whole-``build_history`` CPU is 0.11-4.2 ms, of which
        # ``_serialise_turn`` over ALL turns is 2-35% (0.005-0.72 ms) — the
        # rest is ``estimate_tokens_for_any_turn`` + trim, which a serialise
        # cache cannot remove. A text turn serialises in ~0.24 us because
        # ``_materialise_path_ref_content`` returns ``str`` content untouched,
        # so the up-front cost is materially nonzero ONLY for inline images
        # (synthetic 60 turns / 15x200KB elide: 5.2 ms; 15x1MB: 27 ms). Every
        # such call precedes or accompanies a provider round-trip that is
        # orders of magnitude slower and, in exactly the image-heavy case,
        # uploads that same base64 payload. The saving does not justify a
        # cross-call cache whose staleness would reintroduce the elide/advisor
        # divergence PR-B closed.
        wire_turns = [self._serialise_turn(m) for m in turns]

        # #4403: the elide-check TOTAL — not the serialise pass above, #3185
        # already measured and closed that as cheap — is computed
        # incrementally rather than re-estimating every turn's token cost on
        # every single call. Measured (real litellm tokenizer, the config
        # default use_chars4_estimate=False): 5.13ms/turn -> 559s at
        # 108,896 turns, because build_history() re-summed ALL of them EVERY
        # call. _TOKEN_CACHE_MAXSIZE=8192's per-(model,text) cache cannot
        # help here regardless of size: this loop visits turns in the SAME
        # order every call, so an 8192-entry FIFO always evicts the early
        # turns before a 100k-turn pass reaches them again next call — 100%
        # miss, not a tuning problem. See _incremental_elide_total's own
        # docstring for the invalidation contract.
        total = self._incremental_elide_total(turns, wire_turns, use_chars4=use_chars4)
        # #2957 PR-B (co-vet follow-up): emit the elide side's own internal
        # total as a public P6 audit-event — the ONLY way a test (or an
        # operator inspecting `reyn events`) can observe what THIS method
        # actually counted, as opposed to re-deriving a reference number
        # from its returned wire dicts (which cannot detect a regression in
        # THIS computation itself). None-safe: many test/estimation-path
        # callers construct this buffer with events=None. ``total`` /
        # ``effective_trigger`` are the elide/no-elide decision's own inputs
        # — no conversation content — matching the 0059 §5 audit-payload
        # discipline. See the ``elide_evaluated`` witness in
        # ``tests/runtime/test_2957_prb_elide_advisor_token_unification.py`` for why
        # exercising this requires an UNRESOLVABLE path-ref image fixture,
        # not an ordinary inline one.
        if self._events is not None:
            self._events.emit(
                "elide_evaluated",
                total=total, effective_trigger=effective_trigger,
            )

        if total <= effective_trigger:
            # Window-utilization: full raw conversation fits — no elide.
            selected = wire_turns
        else:
            # Elide the middle: head + optional summary bridge + tail.
            head = trim_head(wire_turns, head_budget, self._model, use_chars4=use_chars4)
            tail = trim_tail(wire_turns, tail_budget, self._model, use_chars4=use_chars4)
            # Overlap guard: dedupe by identity so no turn appears twice.
            head_ids = {id(t) for t in head}
            tail_deduped = [t for t in tail if id(t) not in head_ids]
            summary = self._latest_summary(history)
            if summary:
                summary_text = (
                    summary.content if isinstance(summary.content, str)
                    else json.dumps(summary.content, ensure_ascii=False)
                )
                from reyn.runtime.chat_message import ChatMessage
                bridge_msg = ChatMessage(
                    role="assistant",
                    content=f"[summary of earlier conversation]\n{summary_text}",
                    ts=summary.ts,
                )
                selected = head + [self._serialise_turn(bridge_msg)] + tail_deduped
            else:
                selected = head + tail_deduped

        # ``selected`` is already the wire-dict shape (serialised above) —
        # no second serialise pass needed.
        return self._bound_wire_reasoning(selected)

    def decompose_history_for_retry(
        self,
    ) -> tuple[list[dict], list[dict], list[dict], dict | None, dict[int, int]]:
        """Decompose current history into (head, raw_middle, tail, summary,
        seq_by_id) for retry_loop.

        #1128 step 3: mirrors :meth:`build_history`'s token-budget
        elide threshold (effective_trigger) and exposes the elided ``raw_middle``
        explicitly so the bounded adaptive-shrink ``retry_loop`` (#1125 Item 2)
        can fold it into the running summary under overflow.  ``summary`` is the
        structured dict from the latest persisted summary turn (retry_loop treats
        it as an immutable base).

        When total token estimate <= effective_trigger the full history goes into
        ``head`` with empty ``raw_middle`` / ``tail`` — there is nothing to elide,
        and retry_loop's shrink can still trim ``head``.

        #3599: ``seq_by_id`` maps ``id(wire_dict) -> ChatMessage.seq`` for every
        turn in ``head + raw_middle + tail`` (built off the same ``turns`` /
        ``wire_turns`` pairing already computed here, so no extra serialise
        pass). It lets a caller that only receives a SUBSET of these wire dicts
        (e.g. the force-close wrap-up fallback, which may feed the LLM only
        ``tail`` or neither) recover exactly which seqs that subset covers,
        instead of assuming the full decomposition was used.
        """
        from reyn.services.compaction.engine import (
            estimate_tokens_for_any_turn,
            trim_head,
            trim_tail,
        )

        history = self._history_fn()
        turns = [
            m for m in history
            if m.role in ("user", "assistant", "tool", "agent")
        ]

        # Resolve token budgets from the compaction engine (same as build_history).
        effective_trigger, head_budget, tail_budget = self._resolve_budgets()
        use_chars4 = getattr(self._compaction, "use_chars4_estimate", False)

        # #2957 PR-B: serialise once up front — same canonical-quantity
        # rationale as build_history (see ``_serialise_turn``'s docstring).
        wire_turns = [self._serialise_turn(m) for m in turns]

        # #3599: pair each wire dict back to its source ChatMessage's seq —
        # zip is positionally safe (wire_turns[i] was built FROM turns[i]
        # above, same order, one-to-one). id() keys are only ever looked up
        # against wire dicts drawn from THIS SAME wire_turns list (never
        # across calls / after GC of the list), so no id-reuse hazard.
        seq_by_id: dict[int, int] = {
            id(wt): t.seq for wt, t in zip(wire_turns, turns)
        }

        total = sum(
            estimate_tokens_for_any_turn(wt, self._model, use_chars4=use_chars4)
            for wt in wire_turns
        )

        if total <= effective_trigger:
            # Everything fits — no elide; retry_loop can still trim head.
            head = wire_turns
            raw_middle: list = []
            tail: list = []
        else:
            head = trim_head(wire_turns, head_budget, self._model, use_chars4=use_chars4)
            tail = trim_tail(wire_turns, tail_budget, self._model, use_chars4=use_chars4)
            # raw_middle = turns strictly between head and tail (by identity).
            head_id_set = {id(t) for t in head}
            tail_id_set = {id(t) for t in tail}
            raw_middle = [
                t for t in wire_turns
                if id(t) not in head_id_set and id(t) not in tail_id_set
            ]

        summary_msg = self._latest_summary(history)
        summary_dict: dict | None = None
        if summary_msg is not None:
            structured = (summary_msg.meta or {}).get("structured")
            if isinstance(structured, dict):
                summary_dict = structured
        # #1652/②: bound native reasoning across the ordered carriers (the strip
        # is in-place, so the shared dicts in head/raw_middle/tail are bounded).
        self._bound_wire_reasoning(head + raw_middle + tail)
        return head, raw_middle, tail, summary_dict, seq_by_id

    def build_system_prompt(self) -> str:
        """Return the router system prompt for the current session state.

        ISSUE #4 (PR-N3): used as the ``system_prompt_provider`` for
        :class:`~reyn.services.compaction.engine.CompactionEngine`
        so that T_SP is measured dynamically — operator-editable REYN.md and
        action catalog changes are reflected before each pre-frame budget check.
        """
        from reyn.runtime.router_system_prompt import build_system_prompt
        from reyn.tools.schemes._discovery import tier_wants_discovery_mandate
        from reyn.tools.schemes._universal_sp import build_universal_tool_use_slots
        rh = self._router_host
        univ = bool(getattr(self._action_retrieval, "universal_wrappers_enabled", False))
        # Conservative T_SP estimate: use the router model if known; if not,
        # default to False (= no mandate, slightly under-counts for weak tier
        # but this is an estimation path — conservatively acceptable).
        dm = tier_wants_discovery_mandate(self._model)
        tool_use_sp = build_universal_tool_use_slots(
            universal_wrappers_enabled=univ,
            search_actions_enabled=True,  # conservative: assume enabled (larger SP)
            discovery_mandate=dm,
            has_hot_list_aliases=False,   # conservative: assume no aliases (smaller SP)
            non_interactive=self._non_interactive,
            # #2548 PR-A: include the ## Skills block in the SP-size estimate so
            # the compaction budget accounts for it (same host accessor as live).
            available_skills=(
                getattr(rh, "get_available_skills", lambda: None)()
            ),
        )
        return build_system_prompt(
            agent_name=rh.agent_name,
            agent_role=rh.agent_role,
            available_agents=rh.list_available_agents(),
            memory_index=rh.get_memory_index(),
            file_permissions=rh.get_file_permissions(),
            mcp_servers=rh.get_mcp_servers(),
            web_fetch_allowed=rh.get_web_fetch_allowed(),
            output_language=rh.output_language,
            project_context=rh.get_project_context(),
            tool_use_sp=tool_use_sp,
            # #1652: include the prior-reasoning continuity section so the T_SP
            # estimate (and the override/budget SP path) accounts for it. Host-
            # polymorphic getattr — phase/estimation hosts without the method
            # contribute "" (omit-when-empty, byte-identical).
            reasoning_continuity_section=getattr(
                rh, "reasoning_continuity_section", lambda: ""
            )(),
            non_interactive=self._non_interactive,
        )
