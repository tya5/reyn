"""RouterHistoryBuffer — history slicing and SP assembly for Session.

Extracted from Session (session.py refactor PR-2).  Owns:

  - build_history              — slice history into OpenAI-style messages
  - decompose_history_for_retry — head/raw_middle/tail/summary for retry_loop
  - build_system_prompt        — assemble the router system prompt string
  - _serialise_turn            — materialise one ChatMessage to a wire dict

Also owns the module-level helpers moved out of session.py:

  - _is_force_close_consolidation
  - _materialise_path_ref_content
  - _read_pathref_image

history_fn dependency: a zero-arg callable that returns the raw history list
(all ChatMessages including summaries).  Passed as ``lambda: self.history``
during PR-2; future refactors can re-wire without touching this class.
"""
from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    pass


# ── Standalone helpers (moved from session.py) ────────────────────────────────


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
        model: str,
        events: Any,                      # EventLog — for fallback tokens
        media_store: Any,                 # MediaStore | None — for _serialise_turn
        router_host: Any,                 # RouterHostAdapter — for build_system_prompt
        action_retrieval: Any,            # ActionRetrievalConfig — .universal_wrappers_enabled
        non_interactive: bool,
    ) -> None:
        self._history_fn = history_fn
        self._compaction = compaction
        self._compaction_controller = compaction_controller
        self._model = model
        self._events = events
        self._media_store = media_store
        self._router_host = router_host
        self._action_retrieval = action_retrieval
        self._non_interactive = non_interactive

    # ── Internal helpers ─────────────────────────────────────────────────────

    def _latest_summary(self) -> Any | None:
        """Return the most recent summary message, or None."""
        for m in reversed(self._history_fn()):
            if m.role == "summary":
                return m
        return None

    def _serialise_turn(self, m: Any) -> dict:
        """Serialise one ChatMessage into a litellm-compatible wire dict.

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
        return msg

    def _resolve_budgets(self) -> tuple[int, int, int]:
        """Return (effective_trigger, head_budget, tail_budget)."""
        controller = self._compaction_controller
        engine = getattr(controller, "_engine", None) if controller is not None else None
        budgets = getattr(engine, "budgets", None)
        if budgets is not None:
            return budgets.effective_trigger, budgets.head_budget, budgets.tail_budget
        from reyn.llm.model_budget import get_max_input_tokens
        effective_trigger = get_max_input_tokens(self._model, events=self._events)
        fallback = effective_trigger // 4
        return effective_trigger, fallback, fallback

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
        Only user/agent conversational turns are included; ``summary`` /
        ``skill_event`` remain Reyn-internal and are filtered out.
        """
        from reyn.services.compaction.engine import (
            estimate_tokens_for_turn,
            trim_head,
            trim_tail,
        )

        history = self._history_fn()
        # E-full (#383): include tool-turn entries (= assistant w/ tool_calls,
        # tool responses) in the slice. The wire-shape builder below
        # forwards them as-is to the LLM. ``summary`` / ``skill_event``
        # remain Reyn-internal and filtered out.
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
        _fc_summary = self._latest_summary()
        if _fc_summary is not None and _is_force_close_consolidation(_fc_summary):
            from reyn.chat.session import ChatMessage  # noqa: PLC0415
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
            return [self._serialise_turn(m) for m in (_bridge + _post)]

        effective_trigger, head_budget, tail_budget = self._resolve_budgets()
        use_chars4 = getattr(self._compaction, "use_chars4_estimate", False)

        total = sum(
            estimate_tokens_for_turn(m, self._model, use_chars4=use_chars4)
            for m in turns
        )

        if total <= effective_trigger:
            # Window-utilization: full raw conversation fits — no elide.
            selected = turns
        else:
            # Elide the middle: head + optional summary bridge + tail.
            head = trim_head(turns, head_budget, self._model, use_chars4=use_chars4)
            tail = trim_tail(turns, tail_budget, self._model, use_chars4=use_chars4)
            # Overlap guard: dedupe by identity so no turn appears twice.
            head_ids = {id(t) for t in head}
            tail_deduped = [t for t in tail if id(t) not in head_ids]
            summary = self._latest_summary()
            if summary:
                summary_text = (
                    summary.content if isinstance(summary.content, str)
                    else json.dumps(summary.content, ensure_ascii=False)
                )
                from reyn.chat.session import ChatMessage  # noqa: PLC0415
                bridge = [ChatMessage(
                    role="assistant",
                    content=f"[summary of earlier conversation]\n{summary_text}",
                    ts=summary.ts,
                )]
                selected = head + bridge + tail_deduped
            else:
                selected = head + tail_deduped

        # E-full (#383) pass-through: ChatMessage IS the wire shape, so the
        # builder just serialises each entry into a litellm-compatible
        # message dict. Path-ref content parts (= ``{"type":"image","path":...}``)
        # are materialised to data URLs **at this boundary** so storage
        # stays light and the LLM sees the inline form it expects.
        return [self._serialise_turn(m) for m in selected]

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
            estimate_tokens_for_turn,
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

        total = sum(
            estimate_tokens_for_turn(m, self._model, use_chars4=use_chars4)
            for m in turns
        )

        if total <= effective_trigger:
            # Everything fits — no elide; retry_loop can still trim head.
            head_msgs = turns
            raw_middle_msgs: list = []
            tail_msgs: list = []
        else:
            head_msgs = trim_head(turns, head_budget, self._model, use_chars4=use_chars4)
            tail_msgs = trim_tail(turns, tail_budget, self._model, use_chars4=use_chars4)
            # raw_middle = turns strictly between head and tail (by identity).
            head_id_set = {id(t) for t in head_msgs}
            tail_id_set = {id(t) for t in tail_msgs}
            raw_middle_msgs = [
                t for t in turns
                if id(t) not in head_id_set and id(t) not in tail_id_set
            ]

        summary_msg = self._latest_summary()
        summary_dict: dict | None = None
        if summary_msg is not None:
            structured = (summary_msg.meta or {}).get("structured")
            if isinstance(structured, dict):
                summary_dict = structured
        head = [self._serialise_turn(m) for m in head_msgs]
        raw_middle = [self._serialise_turn(m) for m in raw_middle_msgs]
        tail = [self._serialise_turn(m) for m in tail_msgs]
        return head, raw_middle, tail, summary_dict

    def build_system_prompt(self) -> str:
        """Return the router system prompt for the current session state.

        ISSUE #4 (PR-N3): used as the ``system_prompt_provider`` for
        :class:`~reyn.services.compaction.engine.CompactionEngine`
        so that T_SP is measured dynamically — operator-editable REYN.md and
        skills catalog changes are reflected before each pre-frame budget check.

        Note: ``indexed_sources_section`` is omitted (= None) because this
        method is synchronous and cannot await ``get_source_manifest()``.
        The omission means T_SP is slightly under-counted, which is conservative
        (= compaction triggers slightly more often than strictly necessary).
        The error is small relative to the total context window.
        """
        from reyn.chat.router_system_prompt import build_system_prompt
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
        )
        return build_system_prompt(
            agent_name=rh.agent_name,
            agent_role=rh.agent_role,
            available_skills=rh.list_available_skills(),
            available_agents=rh.list_available_agents(),
            memory_index=rh.get_memory_index(),
            file_permissions=rh.get_file_permissions(),
            mcp_servers=rh.get_mcp_servers(),
            web_fetch_allowed=rh.get_web_fetch_allowed(),
            output_language=rh.output_language,
            project_context=rh.get_project_context(),
            indexed_sources_section=None,
            tool_use_sp=tool_use_sp,
            # #1652: include the prior-reasoning continuity section so the T_SP
            # estimate (and the override/budget SP path) accounts for it. Host-
            # polymorphic getattr — phase/estimation hosts without the method
            # contribute "" (omit-when-empty, byte-identical).
            reasoning_continuity_section=getattr(
                rh, "reasoning_continuity_section", lambda: ""
            )(),
        )
