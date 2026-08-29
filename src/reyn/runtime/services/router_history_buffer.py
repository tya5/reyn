"""RouterHistoryBuffer — history slicing and SP assembly for Session.

Owns:

  - build_history              — slice history into OpenAI-style messages
  - decompose_history_for_retry — head/raw_middle/tail/summary/seq_by_id for retry_loop
  - build_system_prompt        — assemble the router system prompt string
  - _serialise_turn            — materialise one ChatMessage to a wire dict

Also owns the module-level helpers:

  - _materialise_path_ref_content
  - _read_pathref_image
  - _resolve_spilled_content    — #5364 §1.2: lost-file detection for spilled entries

history_fn dependency: a zero-arg callable that returns the raw history list
(all ChatMessages including summaries) — passed in production as
``Session._active_branch_history`` (#2360's WAL-rewind-visibility filter,
NOT a bare ``lambda: self.history``; the two differ after a rewind, and
#4387 Phase B ② made that filtered view able to shrink/reorder call-to-call
by extending ``self.history`` backward on demand — every read of
``self._history_fn()`` in this file re-derives fresh from it, never
assumes append-only or monotonic growth across calls).
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
        from a tool result): read via ``media_store.read_media``.
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
            data_bytes, found = media_store.read_media(path)
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
        → each path-ref is resolved via ``media_store.read_media`` and
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


def _resolve_spilled_content(
    content: "str | list[dict]", meta: Any, project_dir_fn: "Callable[[], Any] | None",
) -> "str | list[dict]":
    """#5364 §1.2: replace a spilled entry's stale ref-preview with an
    explicit "lost" notice once its backing file is actually gone —
    every serialise, via the ONE resolver
    (:func:`reyn.core.offload.history_content_resolve.resolve`).

    No-op (content returned unchanged) unless ``content`` is a ``str``
    AND ``meta`` carries ``SPILLED_META_KEY`` — an unspilled entry's own
    content is already self-sufficient (§1.2's own truth table: the
    ``spilled=False`` row never depends on file existence), so this never
    touches it. ``project_dir_fn is None`` (legacy/test double with no
    workspace access) degrades to "never check" — same fail-closed idiom
    :func:`_refresh_skill_location_tokens` uses for the identical shape.

    Without this, a spilled entry whose backing file was GC'd or never
    persisted keeps showing the model a ``read_file(path=...)`` preview
    naming a file that no longer exists — a silent dangling reference the
    model can only discover by actually trying the read and getting an
    error, rather than being told up front."""
    if not isinstance(content, str) or not isinstance(meta, dict) or project_dir_fn is None:
        return content
    from reyn.runtime.chat_message import CONTENT_REF_META_KEY, SPILLED_META_KEY
    if not meta.get(SPILLED_META_KEY):
        return content
    ref = meta.get(CONTENT_REF_META_KEY)
    if not ref:
        return content
    project_dir = project_dir_fn()
    if project_dir is None:
        return content

    from pathlib import Path as _Path

    from reyn.core.offload.history_content_resolve import HistoryContentEntry, resolve

    def _file_exists(rel_path: str) -> bool:
        return (_Path(project_dir) / rel_path).is_file()

    resolved = resolve(
        HistoryContentEntry(spilled=True, content=content, ref=ref),
        file_exists=_file_exists,
    )
    if resolved.kind == "lost":
        return (
            f"[content lost: the offloaded body at {ref!r} no longer exists "
            "on disk — it may have been deleted or garbage-collected]"
        )
    return content


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
        # #5296 PR-2: session-lived, non-durable reactive-spill overlay —
        # maps a tool-result turn's content hash (``"sha256:<hex>"``, same
        # form/derivation as MediaStore's own ``content_hash``, #5296's own
        # architect ruling: "既存spillの _offload_content_hash と語彙を揃
        # える") to the offloaded preview text that replaces it in every
        # FUTURE ``_serialise_turn`` call for a turn whose content hashes to
        # that key. Applied at the SAME projection stage as the watermark
        # filter (architect ruling, issuecomment-5439234430) — never
        # written to ``history.jsonl``, never mutates a ``ChatMessage`` in
        # place (that would silently change what an already-attached
        # operator sees, a UX change #5296 explicitly does not authorize),
        # never advances the compaction watermark, and does not survive a
        # restart (reversible/idempotent by construction: re-deriving it is
        # just re-spilling the same oversized turn again, not a correctness
        # loss). Keyed by CONTENT, not by index/seq: a compaction pass
        # trimming ``head`` shifts every downstream index, so a positional
        # key would silently point at the wrong turn after the very
        # recovery step (#4954-style durable compaction) this overlay's own
        # caller may trigger next.
        self._spill_overlay: "dict[str, str]" = {}

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
        produce the view 2x (3x when the now-retired elide branch fired,
        #5367 — its 3 return points each called this again) —
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
        elide-threshold check in :meth:`decompose_history_for_retry`
        (#5367 retired the analogous check :meth:`build_history` itself
        used to have) and
        :class:`~reyn.runtime.services.context_budget_advisor.ContextBudgetAdvisor`
        (which measures ``build_history``'s own returned wire dicts) now
        estimate tokens over THIS output, closing a prior circularity where
        the elide side measured serialise-INPUT (raw ChatMessage, pre-image-
        materialisation) while the advisor measured serialise-OUTPUT (the
        wire dicts) — two different quantities for the same
        conversation. Do not reintroduce a second "what does the provider
        see" quantity; measure this method's return value.

        Path-ref content parts (= ``{"type":"image","path":...}``) are
        materialised to data URLs at this boundary so storage stays light
        and the LLM sees the inline form it expects. Shared by
        :meth:`build_history` and :meth:`decompose_history_for_retry` so both
        produce identical wire shapes (the retry_loop decomposition must
        rebuild the same prompt the normal path would have sent).
        """
        # #5531: a summary-role ChatMessage's ``.content`` is a STRUCTURED
        # dict (topic_arc/decisions/...), never plain text — it cannot go
        # through the generic text/tool-call wire-dict construction below
        # at all. Only :meth:`decompose_history_for_retry` ever includes a
        # summary-role turn in what it hands this method (:meth:`build_
        # history`'s own turns filter still excludes it — its own separate
        # bridge-attach step owns that decoration); this branch produces
        # the SAME shape :func:`~reyn.services.compaction.engine.wrap_
        # summary_as_message` builds, so a summary flowing through
        # decompose's head/raw_middle/tail is byte-identical to one built
        # directly for :class:`HistoryChunkToCompact` — one construction,
        # not two that could drift apart.
        if m.role == "summary":
            from reyn.services.compaction.engine import wrap_summary_as_message
            structured = (m.meta or {}).get("structured")
            return wrap_summary_as_message(structured if isinstance(structured, dict) else {})

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
        # #5364 §1.2: fresh, every serialise — a spilled entry's backing
        # file may have been GC'd or never persisted since the last time
        # this turn was serialised (see _resolve_spilled_content's own
        # docstring for why this is not a one-time check at write time).
        content = _resolve_spilled_content(
            content, getattr(m, "meta", None), self._project_dir_fn,
        )
        # #5296 PR-2: apply the reactive-spill overlay, same stage as the
        # watermark filter above — a hit replaces this turn's content with
        # its offloaded preview for every projection from here on (until a
        # future overlay entry supersedes it or the process restarts). The
        # `self._spill_overlay` guard keeps this a no-op fast path (no
        # hashing at all) on every call before the first spill ever fires —
        # the overwhelming common case.
        if self._spill_overlay and isinstance(content, str):
            import hashlib
            content_hash = "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()
            replacement = self._spill_overlay.get(content_hash)
            if replacement is not None:
                content = replacement
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

    def _elide_candidate_turns(self, history: list) -> "tuple[list, int]":
        """Return ``(turns, watermark)`` — role-filtered (E-full #383 —
        user/assistant/tool/agent only, ``summary`` stays Reyn-internal),
        then PERMANENTLY watermark-filtered (#4954(2) — a compacted turn
        never re-enters this projection). *watermark* is also returned
        (not just consumed here) because :meth:`build_history` needs the
        SAME value again afterward, to decide whether to attach the
        summary bridge — returning it avoids a second
        ``_compaction_watermark`` call computing the identical value the
        filter above already derived.

        #5367: this used to also be shared with :meth:`elide_total_and_
        trigger` (#4977, retired — see :meth:`build_history`'s own
        docstring for why the whole elide computation it fed is gone).
        This method itself survives: the permanent-compaction watermark
        filter is a separate concern from elide, still needed to keep a
        compacted turn out of ``build_history``'s own projection."""
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

    # ── Public API ────────────────────────────────────────────────────────────

    def build_history(self) -> list[dict]:
        """Slice history into OpenAI-style messages for RouterLoop.

        #5367 (owner, verbatim: "elide なんて仕様をこっちが提示したことない
        んだってば。なんで残そうとするわけ？") — this method used to also
        do estimate-based window-utilization elide: if the (already
        watermark-filtered) turns' estimated token total exceeded
        ``effective_trigger``, it silently dropped the middle turns (head +
        tail only) before ever sending the request, standing in the
        genuine shrink mechanisms' way. Retired — owner's own framing: the
        two real shrink mechanisms are compact (turn-level, permanent —
        see the watermark filter below) and spill (turn-CONTENT-level,
        durable), and elide was a redundant THIRD path that "solved"
        over-budget by just not sending, never specified anywhere as a
        real mechanism (#5296's own reactive-shrink direction argued the
        opposite: a local token ESTIMATE cannot know what the actual
        provider payload will look like — system prompt, tool schemas,
        transport wrapping, inline media — so acting on the estimate
        risked shrinking a conversation that would have fit fine).

        What now happens on an over-budget history: this method sends the
        full (watermark-filtered) turns raw — no local pre-check. If the
        provider genuinely rejects it, ``router_loop_driver.py``'s own
        REACTIVE shrink ladder (``retry_loop`` — compact via
        ``force_compact_now``, spill via ``_attempt_reactive_spill``) is
        what recovers, on the actual measured overflow, not a local
        estimate; if that ladder is exhausted, ``UnrecoveredError`` is the
        already-designed terminal failure — never a new, silent "sent
        without the middle" degrade. See
        ``tests/runtime/test_5367_reactive_ladder_absorbs_elide_sized_
        overflow.py`` for the witness that this reactive path actually
        catches a history sized the old elide branch used to silently
        absorb.

        #4954(2): PERMANENT compaction is UNCHANGED by this — a turn at or
        below the compaction watermark (``0 < seq <=
        self._compaction_watermark(history)`` — ``seq == 0`` is the #3704
        "no coordinate assigned" sentinel, not the oldest turn, and is
        NEVER excluded; see the filter's own comment below) is still
        excluded from this projection UNCONDITIONALLY — owner's own
        framing: "compaction 結果は永続的に会話を圧縮する" (compaction
        results permanently compact the conversation), and "history.jsonl
        に残すことと llm 見せる会話は分けて考えて" (durable history and
        what the LLM sees are two separate things). The watermark itself
        is NEVER re-derived here — it is read via
        ``self._compaction_watermark``, the same concept
        ``Session._compaction_watermark`` (session.py) already owns.
        ``history.jsonl`` itself is untouched by this — a covered turn is
        excluded from THIS PROJECTION only, still fully readable via
        ``extend_history_backward``.

        The latest summary is ALWAYS part of the projection once the
        watermark is positive — not gated on any elide branch (there is
        none any more); it silently vanishing the instant the conversation
        happened to fit again was exactly the "elide-only decoration"
        shape #4954(2) already closed, and stays closed.

        Returns [{role: 'user'|'assistant', content: str}, ...] ordered
        chronologically. The system prompt is prepended by RouterLoop itself.
        Only user/agent conversational turns are included; the raw
        ``summary`` role itself remains Reyn-internal and is filtered out
        (its content rides the synthetic bridge turn instead).
        """
        history = self._history_fn()
        # #4954(2): permanent compaction — a covered turn never re-enters
        # this projection, whatever the rest of this method does with the
        # remainder.
        turns, watermark = self._elide_candidate_turns(history)

        # #2957 PR-B: serialise ALL candidate turns to their wire-dict shape
        # (see ``_serialise_turn``'s docstring for why this is the
        # canonical quantity to build the returned list from).
        #
        # #3185 (MEASURED, closed won't-fix — do NOT "optimise" this back into
        # a lazy/partial serialise): serialising every CANDIDATE means an
        # image-bearing turn is base64-materialised even when — pre-#5367 —
        # it was about to be elided away; #5367 removes that case (nothing
        # is elided any more), but the measurement still stands: whole-
        # ``build_history`` CPU is 0.11-4.2 ms on real conversations up to
        # 2969 turns, materially nonzero only for inline images, and every
        # such call precedes or accompanies a provider round-trip orders of
        # magnitude slower.
        selected = [self._serialise_turn(m) for m in turns]

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
        can fold it into the running summary under overflow.

        #5531 (owner design dialogue, invariant: a summary represents ONE
        continuous span, placed exactly where that span sat in time) —
        the ``turns`` filter below now INCLUDES ``role == "summary"``
        (previously excluded, without also watermark-filtering — the bug
        lead-coder's own re-check found: a summary's own covered turns
        stayed in ``turns`` too, so "just prepend the summary" was never
        correct). Including it lets the SAME head/raw_middle/tail
        windowing below (``trim_head``/``trim_tail``, already correct for
        every other turn) place the summary at its own natural
        chronological position — no separate line computes WHERE it goes.
        The returned ``summary`` value is then read back out of whichever
        region it landed in (below), never re-derived by a second,
        independent lookup that could disagree with where the window
        actually put it.

        #5531 PR-1/PR-2 boundary (lead-coder ruling, 2026-08-29): PR-1
        only fixes WHERE the summary sits (this filter) — it does NOT
        touch how ``retry_loop`` (``engine.py``) budgets against it (the
        floor formula's own separate ``summary`` term, its own
        independent token estimation) — that reservation-based redesign
        is PR-2's scope. The ``summary`` value returned here still feeds
        that SAME, unchanged formula.

        When total token estimate <= effective_trigger the full history goes into
        ``head`` with empty ``raw_middle`` / ``tail`` — there is nothing to elide,
        and retry_loop's shrink can still trim ``head``.

        #3599: ``seq_by_id`` maps ``id(wire_dict) -> ChatMessage.seq`` for every
        turn in ``head + raw_middle + tail`` (built off the same ``turns`` /
        ``wire_turns`` pairing already computed here, so no extra serialise
        pass). It lets a caller that only receives a SUBSET of these wire dicts
        (e.g. the force-close wrap-up fallback, which may feed the LLM only
        ``tail`` or neither) recover exactly which seqs that subset covers,
        instead of assuming the full decomposition was used. A summary-role
        turn carries a real ``seq`` like any other role (#3704 — every
        persisted entry gets one, regardless of role), so it needs no
        special case here: it flows through the same ``zip`` as everything
        else.
        """
        from reyn.services.compaction.engine import (
            SUMMARY_MESSAGE_ROLE,
            estimate_tokens_for_any_turn,
            trim_head,
            trim_tail,
        )

        history = self._history_fn()
        turns = [
            m for m in history
            if m.role in ("user", "assistant", "tool", "agent", SUMMARY_MESSAGE_ROLE)
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

    def compaction_watermark(self) -> int:
        """#5364 §1.6 (architect review): public accessor for
        :meth:`_compaction_watermark` — the reduction axis's OTHER
        progress witness, alongside :meth:`is_already_spilled`'s spill
        axis. A STRUCTURAL fact (the latest summary's
        ``covers_through_seq``), never a byte count — the compaction axis
        is compared as "did the watermark advance", the same way the
        spill axis is compared as "did a candidate get consumed", never
        by re-measuring wire bytes for either."""
        return self._compaction_watermark()

    def is_already_spilled(self, content: str) -> bool:
        """#5364 §1.6: True if ``content`` IS a previously-produced spill
        preview (a value ``spill_turn_content`` itself once returned),
        rather than an original body that merely happens to look similar.

        A candidate whose CURRENT (overlay-substituted) content already
        satisfies this must never be offered to ``spill_turn_content``
        again — that call is not idempotent on a preview: a preview's own
        token count almost always still exceeds ``cap_tokens=1``, so
        re-offloading it produces ANOTHER, different preview (a new
        ``seq``, a new path) rather than returning the input unchanged.
        Without this check, ``_attempt_reactive_spill`` would treat that
        as fresh progress forever — an infinite loop, never reaching
        #5364 §1.6's failure predicate (candidates exhausted). Checked by
        VALUE (this method takes no turn identity — the same content
        string could arrive from a different candidate object across
        calls, e.g. after a `decompose_history_for_retry` re-scan)."""
        return content in self._spill_overlay.values()

    def spill_turn_content(
        self, content: str, *, chain_id: str = "", tool: str = "tool", seq: int = 1,
    ) -> "str | None":
        """#5296 PR-2: reactively spill one already-serialised tool-result
        wire string — offload it via the SAME mechanism the existing
        write-time cap already uses (``tool_result_cap.cap_tool_result_
        content`` + ``MediaStore.save_tool_result``, architect ruling:
        "既存機構を再利用"), record the resulting overlay entry so every
        FUTURE ``_serialise_turn`` of a turn with this exact content
        returns the offloaded preview instead, and return that preview
        text (``None`` if no ``media_store`` is configured — the same
        no-op degrade the write-time cap already has for that case; the
        caller treats that as "no progress" and escalates).

        ``cap_tokens=1`` forces the offload branch unconditionally — this
        method is called only once a caller has ALREADY decided (by
        ordering: oldest/largest first, #5296's own contract ②) that THIS
        turn should be spilled; ``cap_tool_result_content`` itself has no
        "force offload regardless of size" mode, only "offload if over
        cap_tokens", so a threshold no real content can be under is how
        this reuses that function without adding a second offload path.
        No new threshold config here — #5296's own contract explicitly
        rules that out ("閾値configを作らない").

        Deliberately does NOT check ``offload.enabled`` (the write-time
        cap's own debug lever, ``ContextBudgetAdvisor.cap_tool_result``'s
        first check) — spill is a REACTIVE overflow-recovery operation,
        not a proactive budget-shaping one (architect review); that flag
        exists to let an operator disable the routine per-turn offload
        decision, not to also silence the last lever between a 413 and a
        failed turn.
        """
        if self._media_store is None:
            return None
        import hashlib

        from reyn.runtime.services.tool_result_cap import (
            TRIGGER_OVERFLOW,
            cap_tool_result_content,
        )

        def _save(c: "str | dict", **kw: Any) -> dict:
            # #5387: ``chain_id`` arrives via ``**kw`` now — forwarded by
            # ``cap_tool_result_content`` from the ``chain_id=chain_id``
            # passed below (this method's OWN param, not a separate
            # value) — NOT hardcoded here too, which would collide
            # ("got multiple values for keyword argument 'chain_id'").
            return self._media_store.save_tool_result(
                c, tool=tool, seq=seq, **kw
            )

        replacement = cap_tool_result_content(
            content,
            cap_tokens=1,
            model=self._model,
            trigger=TRIGGER_OVERFLOW,
            save_fn=_save,
            use_chars4=getattr(self._compaction, "use_chars4_estimate", False),
            events=self._events,
            chain_id=chain_id,
        )
        if replacement == content:
            # cap_tool_result_content's own no-op paths (cap<=0, or the
            # store write itself somehow returned the input unchanged) —
            # nothing was actually offloaded, so no overlay entry to add.
            return None
        content_hash = "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()
        self._spill_overlay[content_hash] = replacement
        return replacement

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
