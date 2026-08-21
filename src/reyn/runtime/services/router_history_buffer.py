"""RouterHistoryBuffer — history slicing and SP assembly for Session.

Owns:

  - build_history              — slice history into OpenAI-style messages
  - decompose_history_for_retry — head/raw_middle/tail/summary/seq_by_id for retry_loop
  - build_system_prompt        — assemble the router system prompt string
  - _serialise_turn            — materialise one ChatMessage to a wire dict

Also owns the module-level helpers:

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

# #4477: warn-once cache for the compaction-batch/head+tail-budget invariant
# below — the 4th instance of this SSoT's resource/budget comparison class.
# Reset in tests via `_compaction_batch_budget_warned.clear()`.
_compaction_batch_budget_warned: "set[tuple[str, str | None]]" = set()


def _check_resource_within_budget(
    model: str,
    phase: "str | None",
    effective_trigger: int,
    events: Any,
    read_cap_config: Any = None,
) -> None:
    """#4381 PR-1/PR-5: the invariant this SSoT enforces — "a result that
    passed the resource boundary must not exceed the budget boundary after
    conversion" (architect design, #4381). Two independent read-cap-vs-
    spill-cap mismatches (#4381's own reported incident, #4432's spill-loop
    guard) trace to this SAME class going unchecked: the resource boundary
    (``control_ir_inline_cap``, BYTES as of PR-5 — memory/transfer-scoped,
    model-INDEPENDENT config value as of PR-5) and the budget boundary
    (``effective_trigger``, TOKENS — the model's context window) are never
    compared anywhere before PR-1, so a resource-bounded result that looks
    "safe" can still overflow the budget boundary once converted.

    ``read_cap_config`` (PR-5): the ``ReadCapConfig`` to check against —
    threaded from ``RouterHistoryBuffer`` (the caller with a live config
    reference). ``None`` (a caller with no config threaded, e.g.
    ``ContextBudgetAdvisor``'s own call today) falls back to
    ``control_ir_inline_cap``'s own ``config=None`` default
    (``MAX_CONTROL_IR_RESULT_INLINE_BYTES``) — a known, flagged gap, not a
    silent one: a config value that DIFFERS from the shipped default is
    only checked via the caller that threads it.

    The conversion point is ``context_builder.INLINE_CAP_BYTES_PER_TOKEN``
    — the ONE named bytes→tokens conversion (architect: name it once,
    don't re-derive the same ratio elsewhere; PR-5 renamed this from
    ``INLINE_CAP_CHARS_PER_TOKEN`` since the resource bound switched from
    chars to bytes — reusing the old name under a new unit would silently
    reopen the exact drift class this constant exists to prevent). Rounded
    UP (ceil): understating the resource bound's token cost would make
    this check too permissive, the wrong direction for a safety check.

    Warn-once per ``(model, phase)`` (not per call — this SSoT is called on
    every trigger resolution, so warning every time would flood the log)
    via a ``resource_cap_exceeds_budget_trigger`` audit-event. Detection
    only — no value is clamped.
    """
    from reyn.core.context_builder import (
        INLINE_CAP_BYTES_PER_TOKEN,
        control_ir_inline_cap,
    )

    resource_bound_bytes = control_ir_inline_cap(read_cap_config)
    resource_bound_tokens = -(-resource_bound_bytes // INLINE_CAP_BYTES_PER_TOKEN)  # ceil
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
            resource_bound_bytes=resource_bound_bytes,
            resource_bound_tokens=resource_bound_tokens,
            effective_trigger=effective_trigger,
        )


def _check_compaction_batch_within_budget(
    model: str, phase: "str | None", head_budget: int, tail_budget: int, events: Any,
) -> None:
    """#4477: the 4th instance of the resource/budget comparison class
    ``_check_resource_within_budget`` (#4381 PR-1) already established —
    same conversion point (``INLINE_CAP_BYTES_PER_TOKEN``), same warn-once
    shape, different pair: ``read_history_after``'s own per-call byte cap
    (``COMPACTION_BATCH_MAX_BYTES`` — a RESOURCE bound, #4472/#4475) vs
    ``head_budget + tail_budget`` (a BUDGET bound, model-context-window-
    derived, #4431's role split).

    **Why this check exists at all — measured, not assumed** (architect's
    #4475 follow-up review + this issue's own explicit first task, before
    any warn mechanism was written): if the batch cap is smaller than
    ``head_budget + tail_budget``'s own combined token footprint (in
    bytes), a compaction pass produces ZERO candidates every time — head
    and tail trimming alone consume the whole small batch, leaving no
    middle. Worse than merely "no progress": zero candidates means no
    summary, means ``covers_through_seq`` never advances, means the NEXT
    pass reads the IDENTICAL window and produces the IDENTICAL zero — a
    genuine, permanent STALL, the exact class #4470/#4471/#4472 exist to
    close, reachable through this specific resource/budget combination.

    Confirmed LIVE (not theoretical) against this repo's own installed
    litellm catalog (verified 2026-08-13): ``component_weights``'s shipped
    default (head=10, tail=15, of 100 total ⇒ 25% combined) times
    ``INLINE_CAP_BYTES_PER_TOKEN`` (4) means the worst-case
    ``head_budget + tail_budget`` in BYTES equals the model's own
    ``max_input_tokens`` numerically (0.25 × 4 = 1). At least 5 models in
    the installed litellm catalog exceed ``COMPACTION_BATCH_MAX_BYTES``
    (8 MiB = 8,388,608) at this weighting — e.g.
    ``oci/meta.llama-4-scout-17b-16e-instruct`` at 10,485,760 tokens (a
    real, currently-selectable model, not a hypothetical) — so this is a
    REACHABLE misconfiguration, not a defensive-only guard.

    Warn-once per ``(model, phase)``, same reasoning as the sibling check
    above (a high-frequency SSoT). Detection only — no value is clamped;
    unlike the resource-bound/budget check above, there is no obvious safe
    clamp direction here (shrinking the batch cap further only worsens the
    stall; growing it is an operator/model-choice decision, not something
    this call site should silently do).
    """
    from reyn.core.context_builder import INLINE_CAP_BYTES_PER_TOKEN
    from reyn.runtime.history_tail_reader import COMPACTION_BATCH_MAX_BYTES

    combined_tokens = head_budget + tail_budget
    combined_bytes = combined_tokens * INLINE_CAP_BYTES_PER_TOKEN
    if combined_bytes <= COMPACTION_BATCH_MAX_BYTES:
        return
    key = (model, phase)
    if key in _compaction_batch_budget_warned:
        return
    _compaction_batch_budget_warned.add(key)
    if events is not None:
        events.emit(
            "compaction_batch_cap_below_head_tail_budget",
            model=model, phase=phase or "",
            head_budget=head_budget, tail_budget=tail_budget,
            combined_bytes=combined_bytes,
            compaction_batch_max_bytes=COMPACTION_BATCH_MAX_BYTES,
        )


def resolve_effective_trigger_and_budgets(
    compaction_controller: Any,
    model: str,
    events: Any,
    *,
    phase: "str | None" = None,
    read_cap_config: Any = None,
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

    #4477: also the SSoT for a FOURTH instance of the same class —
    ``_check_compaction_batch_within_budget`` — comparing
    ``head_budget + tail_budget`` (already computed here) against
    #4472/#4475's own compaction-batch byte cap. Confirmed reachable
    (see that function's own docstring for the live measurement) before
    being added — the class's own established discipline: don't build an
    unreachable-machinery warn.

    ``read_cap_config`` (#4381 PR-5): the ``ReadCapConfig`` to check the
    resource bound against — threaded from ``RouterHistoryBuffer``, which
    has a live config reference; ``ContextBudgetAdvisor``'s own call
    passes none today (falls back to the shipped default — see
    ``_check_resource_within_budget``'s own docstring for what that means).
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
    _check_resource_within_budget(model, phase, effective_trigger, events, read_cap_config)
    # #4477: 4th instance of the resource/budget comparison class — the
    # compaction batch's own byte cap vs head+tail's combined token budget.
    _check_compaction_batch_within_budget(model, phase, head_budget, tail_budget, events)
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
        universal_wrappers_enabled: bool,  # #4552 PR-3: moved from ActionRetrievalConfig
        non_interactive: bool,
        reasoning: Any = None,            # ReasoningConfig — .continuity / .recent_turns (#1652/②)
        project_dir_fn: "Callable[[], Any] | None" = None,  # #3629: zero-arg → CURRENT workspace base_dir
        read_cap: Any = None,             # #4381 PR-5: ReadCapConfig — the resource bound to check budgets against
    ) -> None:
        self._history_fn = history_fn
        self._compaction = compaction
        self._compaction_controller = compaction_controller
        self._model_fn = model_fn
        self._events = events
        self._media_store = media_store
        self._router_host = router_host
        self._universal_wrappers_enabled = universal_wrappers_enabled
        self._non_interactive = non_interactive
        # #4381 PR-5: threaded into resolve_effective_trigger_and_budgets's
        # resource/budget invariant check (_check_resource_within_budget).
        self._read_cap = read_cap
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

    def _compaction_watermark(self, history: "list | None" = None) -> int:
        """#4954(2): the latest summary's ``covers_through_seq`` (0 if none
        yet) — seqs at or below this are considered compacted out of the
        LLM-facing projection. Consumes ``Session._compaction_watermark``'s
        own concept (``session.py:7042-7052``) via THIS class's own
        ``_latest_summary`` rather than re-deriving a second "what counts
        as compacted" notion — same ``(latest.meta or {}).get(
        "covers_through_seq", 0)`` read, same branch/rewind safety (both
        route through ``history``, which callers here already resolve via
        ``self._history_fn()`` — ``Session._active_branch_history``, fresh
        per call)."""
        latest = self._latest_summary(history)
        return int((latest.meta or {}).get("covers_through_seq", 0)) if latest is not None else 0

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
            read_cap_config=self._read_cap,
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

    def _elide_candidate_turns(self, history: list) -> "tuple[list, int]":
        """Return ``(turns, watermark)`` — the turns considered for the
        #1128/#4954(2) elide-threshold decision: role-filtered (E-full
        #383 — user/assistant/tool/agent only, ``summary`` stays
        Reyn-internal), then PERMANENTLY watermark-filtered (#4954(2) — a
        compacted turn never re-enters this projection, regardless of
        whether the elide branch fires). *watermark* is also returned
        (not just consumed here) because :meth:`build_history` needs the
        SAME value again afterward, to decide whether to attach the
        summary bridge — returning it avoids a second
        ``_compaction_watermark`` call computing the identical value the
        filter above already derived.

        Factored out of ``build_history`` (#4977) so it and
        :meth:`elide_total_and_trigger` share ONE implementation of this
        filter instead of two copies that could drift apart — the same
        concern the watermark predicate's own comment below already
        flags for a 3rd copy appearing anywhere in the codebase; this
        keeps THIS file at one, not two."""
        turns = [
            m for m in history
            if m.role in ("user", "assistant", "tool", "agent")
        ]
        # #4954(2) TESTS-READ finding (lead-coder): ``seq == 0`` is the
        # #3704 sentinel for "no coordinate assigned" (pre-#3704 legacy
        # history), NOT "oldest turn" — ``chat_message.py``'s own field
        # comment. A bare ``m.seq > watermark`` treats that sentinel as
        # older than everything, permanently dropping every legacy turn
        # the instant ANY watermark exists. Worse: such a turn was NEVER
        # a compaction candidate either (``compaction_controller.py``'s
        # own candidate filter is ``t.seq > prev_cover``, always false at
        # seq=0) — so it was never summarised, and this exclusion would
        # be the only place that ever stopped sending it: silent,
        # permanent content loss. ⚠️ Reachability is UNMEASURED (whether
        # any session with real #3704-pre-fix legacy history still
        # exists is environment-dependent) — closed anyway because the
        # damage shape (silent, permanent loss) costs more than this one
        # condition does; do not read this comment as "confirmed
        # reachable" (#4941's declaration≠guarantee caution).
        #
        # NOT a new predicate: ``m.seq == 0 or m.seq > watermark`` is the
        # EXACT expression ``Session``'s own #4468 security-latch scan
        # already uses (session.py:3074, same
        # ``self._compaction_watermark()`` value) — sharing the VALUE
        # without sharing how it's READ is exactly how this drifted.
        # ⚪ This predicate now exists in 2 places (session.py:3074, and
        # this method — #4977 collapsed the 2 copies THIS FILE used to
        # carry down to 1); if a 3rd appears anywhere, that is the point
        # to factor it into one shared function (architect, non-blocking)
        # rather than copying a 3rd time.
        watermark = self._compaction_watermark(history)
        if watermark > 0:
            turns = [m for m in turns if m.seq == 0 or m.seq > watermark]
        return turns, watermark

    def elide_total_and_trigger(self) -> "tuple[int, int]":
        """Return ``(total, effective_trigger)`` — #4403's own incremental
        elide-check computation, the SAME one :meth:`build_history` uses
        for its elide/no-elide decision.

        #4977 (owner + architect ruling): promoted from a private-only
        computation whose only external observation point used to be the
        (now-retired) ``elide_evaluated`` audit-event. Root cause named
        by architect: an audit-event is operator/replay vocabulary, not
        a test seam — using one to make private state observable to
        tests was itself the mistake, not a missing opt-in on it. A
        ``snapshot()``-style container holding "the last computed value"
        was considered and rejected (architect: a second place to hold
        the same fact, which can go stale relative to the first) —
        instead, the computation itself becomes a normal public method.

        Goes through the SAME pieces :meth:`build_history` itself calls —
        :meth:`_elide_candidate_turns` for the turns filter, then
        :meth:`_incremental_elide_total` (the #4403 incremental cache) —
        not a parallel or bypassing recomputation. A caller (production
        or a test) exercising this method exercises the exact cache path
        production relies on; a test could not tell this method's answer
        apart from what ``build_history`` itself just decided."""
        history = self._history_fn()
        turns, _watermark = self._elide_candidate_turns(history)
        effective_trigger, _head_budget, _tail_budget = self._resolve_budgets()
        use_chars4 = getattr(self._compaction, "use_chars4_estimate", False)
        wire_turns = [self._serialise_turn(m) for m in turns]
        total = self._incremental_elide_total(turns, wire_turns, use_chars4=use_chars4)
        return total, effective_trigger

    # ── Public API ────────────────────────────────────────────────────────────

    def build_history(self) -> list[dict]:
        """Slice history into OpenAI-style messages for RouterLoop.

        #4954(2): PERMANENT compaction. A turn at or below the compaction
        watermark (``0 < seq <= self._compaction_watermark(history)`` —
        ``seq == 0`` is the #3704 "no coordinate assigned" sentinel, not
        the oldest turn, and is NEVER excluded; see the filter's own
        comment below) is excluded from this projection UNCONDITIONALLY,
        before any budget/elide reasoning runs — owner's own framing: "compaction
        結果は永続的に会話を圧縮する" (compaction results permanently
        compact the conversation), and "history.jsonl に残すことと llm
        見せる会話は分けて考えて" (durable history and what the LLM sees
        are two separate things). Before this, a covered turn's fate
        depended entirely on whether ``total <= effective_trigger`` this
        call — a conversation that is byte-heavy but token-light (e.g.
        materialised images, which cost a FIXED token estimate regardless
        of actual size — ``_IMAGE_FIXED_TOKEN_COST``) could stay under
        ``effective_trigger`` forever, so its covered turns were resent
        raw on EVERY turn, permanently duplicating whatever the summary
        already represents and defeating the point of having compacted at
        all (#4954's own real-machine symptom). The watermark itself is
        NEVER re-derived here — it is read via ``self._compaction_watermark``,
        the same concept ``Session._compaction_watermark`` (session.py)
        already owns, not a second "what counts as compacted" notion.
        ``history.jsonl`` itself is untouched by this — a covered turn is
        excluded from THIS PROJECTION only, still fully readable via
        ``extend_history_backward``.

        The latest summary is ALWAYS part of the projection once the
        watermark is positive (never gated on which branch below fires —
        an elide-only bridge would make a covered range's summary
        disappear the instant the (now watermark-shrunk) conversation fits
        the budget again, silently losing the represented content instead
        of just not re-sending its raw form).

        #1128 step 3 (Fork B — window-utilization-first), applied AFTER the
        watermark filter above: the elide point coincides with
        ``effective_trigger`` (the existing pre-frame compaction trigger)
        instead of the old turn-count head_size/tail_size.

        - If total token estimate (of the ALREADY watermark-filtered turns)
          <= effective_trigger: return them all raw (no further elide, no
          duplication) — plus the summary bridge, if any.
        - Else: elide the middle of the (already watermark-filtered) turns
          — head (trim_head) + tail (trim_tail) — plus the summary bridge,
          if any. The pre-frame guard ``maybe_force_compact`` has already
          compacted the middle before this runs, so the elide point is
          structurally aligned.

        Overlap guard: if trim_head and trim_tail collectively cover all
        turns (the chat is small relative to budgets but total > trigger —
        unlikely but possible with large single turns), deduplication by
        identity ensures no turn appears twice.

        Returns [{role: 'user'|'assistant', content: str}, ...] ordered
        chronologically. The system prompt is prepended by RouterLoop itself.
        Only user/agent conversational turns are included; the raw
        ``summary`` role itself remains Reyn-internal and is filtered out
        (its content rides the synthetic bridge turn instead).
        """
        from reyn.services.compaction.engine import trim_head, trim_tail

        history = self._history_fn()
        # #4954(2): permanent compaction — filter BEFORE any budget/elide
        # reasoning below (both the `total` this method computes for its
        # own elide decision, and the trim/elide steps that decision
        # gates), so a covered turn never contributes to either. Position
        # matters, two ways: (a) filtering AFTER `total`'s computation
        # would let `total` count content this method is about to not
        # send, running the elide yes/no decision against a payload that
        # was never real; (b) filtering happens ONCE — inside
        # ``_elide_candidate_turns`` — BEFORE `wire_turns` is built from
        # it below — so the two stay positionally paired (architect
        # review: filtering either list independently, or at two
        # different points, would let them drift silently out of
        # correspondence).
        #
        # Cost note for `_incremental_elide_total` (#4403) below: when a
        # NEW compaction advances `watermark`, `turns` shrinks relative to
        # the previous call — the cache's structural validity check
        # (length + boundary seq) correctly detects this as it would a
        # rewind, and falls back to a full O(n) recompute for that one
        # call (its own docstring: "a wrong invalidation guess costs CPU,
        # never correctness") — bounded to once per compaction, not a
        # per-turn regression.
        turns, watermark = self._elide_candidate_turns(history)

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
        # #4977: this method's own internal `total`/`effective_trigger` used
        # to also be emitted here as a public P6 audit-event
        # (`elide_evaluated`) so a test (or an operator inspecting `reyn
        # events`) could observe what THIS method actually counted, as
        # opposed to re-deriving a reference number from its returned wire
        # dicts (which cannot detect a regression in THIS computation
        # itself). Retired (owner + architect ruling): the payload was
        # never an operation record — an internal estimate/threshold pair
        # with zero transport consumers — and using an audit-event as a
        # test observation seam was itself the mistake (CLAUDE.md: an
        # absence with neither a public surface nor a `snapshot()`-style
        # read IS the finding, not something to paper over with a new
        # audit-event kind). See :meth:`elide_total_and_trigger` — the
        # SAME computation, now a normal public method a test calls
        # directly instead of observing indirectly through the audit log.

        if total <= effective_trigger:
            # Window-utilization: the (already watermark-filtered) turns
            # fit raw — no further elide.
            selected = wire_turns
        else:
            # Elide the middle: head + tail (summary bridge, if any, is
            # attached below — unconditionally, not gated on this branch).
            head = trim_head(wire_turns, head_budget, self._model, use_chars4=use_chars4)
            tail = trim_tail(wire_turns, tail_budget, self._model, use_chars4=use_chars4)
            # Overlap guard: dedupe by identity so no turn appears twice.
            head_ids = {id(t) for t in head}
            tail_deduped = [t for t in tail if id(t) not in head_ids]
            selected = head + tail_deduped

        # #4954(2): the summary bridge is now attached HERE, unconditionally
        # once `watermark > 0` — not only on the elide branch above. Before
        # this fix the bridge only appeared when trim/elide fired; the
        # instant the (now watermark-shrunk) conversation fit the budget
        # again, the summary silently vanished from the projection even
        # though it still represents real, permanently-excluded content —
        # the "elide-only decoration" this PR's own issue explicitly
        # forbids reintroducing.
        if watermark > 0:
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
                selected = [self._serialise_turn(bridge_msg)] + selected

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
        univ = self._universal_wrappers_enabled
        # Conservative T_SP estimate: use the router model if known; if not,
        # default to False (= no mandate, slightly under-counts for weak tier
        # but this is an estimation path — conservatively acceptable).
        dm = tier_wants_discovery_mandate(self._model)
        tool_use_sp = build_universal_tool_use_slots(
            universal_wrappers_enabled=univ,
            search_actions_enabled=True,  # conservative: assume enabled (larger SP)
            discovery_mandate=dm,
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
