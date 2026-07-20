"""RouterHistoryBuffer — history slicing and SP assembly for Session.

Owns:

  - build_history              — slice history into OpenAI-style messages
  - decompose_history_for_retry — head/raw_middle/tail/summary for retry_loop
  - build_system_prompt        — assemble the router system prompt string
  - _serialise_turn            — materialise one ChatMessage to a wire dict

Also owns the module-level helpers:

  - _is_force_close_consolidation
  - _materialise_path_ref_content
  - _read_pathref_image

history_fn dependency: a zero-arg callable that returns the raw history list
(all ChatMessages including summaries), passed as ``lambda: self.history``.
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


def resolve_effective_trigger_and_budgets(
    compaction_controller: Any, model: str, events: Any,
) -> "tuple[int, int, int]":
    """Return ``(effective_trigger, head_budget, tail_budget)`` — #2957 PR-B
    single SSoT for this lookup.

    Before PR-B, :class:`RouterHistoryBuffer` (``_resolve_budgets``) and
    :class:`~reyn.runtime.services.context_budget_advisor.ContextBudgetAdvisor`
    (``_get_effective_trigger``) each reimplemented the identical
    ``compaction_controller._engine.budgets`` lookup + ``get_max_input_tokens``
    fallback independently — a duplication that could silently drift (one
    site's fallback changing without the other). Both now delegate here.
    """
    engine = getattr(compaction_controller, "_engine", None) if compaction_controller is not None else None
    budgets = getattr(engine, "budgets", None)
    if budgets is not None:
        return budgets.effective_trigger, budgets.head_budget, budgets.tail_budget
    from reyn.llm.model_budget import get_max_input_tokens
    effective_trigger = get_max_input_tokens(model, events=events)
    fallback = effective_trigger // 4
    return effective_trigger, fallback, fallback


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
        from reyn.services.compaction.engine import (
            estimate_tokens_for_any_turn,
            trim_head,
            trim_tail,
        )

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
        # consolidation in history order), NOT seq>covers — assistant/tool turns
        # keep seq=0 (only user/agent get a monotonic seq), so a seq filter would
        # wrongly drop post-handoff assistant replies. GATED to force-close
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
        wire_turns = [self._serialise_turn(m) for m in turns]

        total = sum(
            estimate_tokens_for_any_turn(wt, self._model, use_chars4=use_chars4)
            for wt in wire_turns
        )
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
        # ``tests/test_2957_prb_elide_advisor_token_unification.py`` for why
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
    ) -> tuple[list[dict], list[dict], list[dict], dict | None]:
        """Decompose current history into (head, raw_middle, tail, summary) for retry_loop.

        #1128 step 3: mirrors :meth:`build_history`'s token-budget
        elide threshold (effective_trigger) and exposes the elided ``raw_middle``
        explicitly so the bounded adaptive-shrink ``retry_loop`` (#1125 Item 2)
        can fold it into the running summary under overflow.  ``summary`` is the
        structured dict from the latest persisted summary turn (retry_loop treats
        it as an immutable base).

        When total token estimate <= effective_trigger the full history goes into
        ``head`` with empty ``raw_middle`` / ``tail`` — there is nothing to elide,
        and retry_loop's shrink can still trim ``head``.
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
        return head, raw_middle, tail, summary_dict

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
