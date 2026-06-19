"""CompactionController — synchronous head/body/tail compaction.

Extracted from Session (FP-0019 Wave 1).  Drives OS-internal compaction
(PR-N3: direct Python helper, no skill/phase overhead) via
:meth:`force_compact_now`, the synchronous pre-frame guard path.

#1128 PR-a: the background fire-and-forget path (``spawn_maybe`` →
``_maybe_compact``, the 30K-absolute ``trigger_total_tokens`` trigger) was
removed. Auto-compaction is now driven solely by the synchronous pre-frame
guard (``ContextBudgetAdvisor.maybe_force_compact`` → :meth:`force_compact_now`,
window-relative ``effective_trigger``, token-budget candidate selection per
step 3), plus on-demand (the ``compact`` op / ``/compact``) and the
``retry_loop`` overflow backstop. With no background task, compaction always
runs synchronously inside the serial router handler.

All event emissions go through the injected ``event_log``; no silent
state changes (P6).  Business logic lives entirely here; Session
delegates via :meth:`force_compact_now` (P3).
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Callable

from reyn.config import CompactionConfig
from reyn.core.events.events import EventLog
from reyn.services.compaction.engine import (
    CompactionEngine,
    HistoryChunkToCompact,
    trim_head,
    trim_tail,
)

if TYPE_CHECKING:
    from reyn.runtime.chat_message import ChatMessage

logger = logging.getLogger(__name__)


def _estimate_tokens(text: str) -> int:
    """Cheap chars/4 token estimate. Same heuristic used by other Reyn paths."""
    return max(1, len(text or "") // 4)


def _turn_to_compactor_input(t: "ChatMessage") -> dict:
    """Serialise a ChatMessage into the compactor's ``new_turns`` shape.

    Post-PR-E1 (issue #383) the history may contain ``assistant`` entries
    with ``tool_calls``, ``tool`` entries with ``tool_call_id`` + ``name``,
    and ``user``/``assistant`` entries with multimodal ``content`` lists.
    The compactor skill needs enough structure to reason about tool
    activity in ``artifacts_referenced`` while staying within token caps.

    Shape we emit per turn:
      {role, text, seq, [tool_calls], [tool_call_id], [tool_name]}

    ``text`` is the derived text view (= str content or first text part
    from a list content). Tool fields are only included on the entries
    where they're set.
    """
    out: dict = {"role": t.role, "text": t.text, "seq": t.seq}
    if getattr(t, "tool_calls", None):
        # Compact representation: function names + arg-string lengths.
        # Avoid sending raw arg JSON since it can be large and the
        # compactor only needs the structural shape ("LLM called fn X
        # with N chars of args"). The skill's ``artifacts_referenced``
        # rule decides whether to surface the call.
        out["tool_calls"] = [
            {
                "name": (tc.get("function") or {}).get("name", ""),
                "args_chars": len((tc.get("function") or {}).get("arguments", "") or ""),
            }
            for tc in t.tool_calls
            if isinstance(tc, dict)
        ]
    if getattr(t, "tool_call_id", None):
        out["tool_call_id"] = t.tool_call_id
    if getattr(t, "name", None):
        out["tool_name"] = t.name
    return out


class CompactionController:
    """Background head/body/tail compaction service.

    Parameters
    ----------
    event_log:
        Session-scoped :class:`~reyn.core.events.events.EventLog`.  All
        compaction events are emitted here.
    config:
        :class:`~reyn.config.CompactionConfig` — thresholds and sizing.
    history_access:
        Zero-argument callable that returns a read-only snapshot of the
        current chat history (``list[ChatMessage]``).
    latest_summary:
        Zero-argument callable that returns the most recent ``"summary"``
        :class:`~reyn.runtime.chat_message.ChatMessage`, or ``None``.
    compaction_engine:
        :class:`~reyn.services.compaction.engine.CompactionEngine`
        that owns the single LLM call (PR-N3: OS-internal, no skill/phase).
    history_appender:
        Callable ``(ChatMessage) -> None`` that appends a message to the
        persisted history.  Wraps ``Session._append_history``.
    make_summary_message:
        Callable ``(rendered_text, structured, covers_through_seq) ->
        ChatMessage`` that constructs the summary ``ChatMessage`` to be
        appended.  Provided by the session so the controller does not
        need to import ``ChatMessage`` or ``_now_iso`` directly.
    render_summary:
        Callable ``(structured: dict) -> str`` that renders a structured
        summary dict to a storage-friendly text blob.
    merge_action_usage:
        Optional sink for the per-agent
        :class:`~reyn.tools.action_usage_tracker.ActionUsageTracker`.
        When set, the controller invokes
        ``merge_action_usage(candidates)`` with the list of
        ``ChatMessage`` instances being folded into the summary; the
        sink is responsible for extracting tool-call records and
        forwarding them to the tracker. Ignored when ``None``
        (= session has no tracker configured).
    """

    def __init__(
        self,
        *,
        event_log: EventLog,
        config: CompactionConfig,
        history_access: Callable[[], list[ChatMessage]],
        latest_summary: Callable[[], ChatMessage | None],
        compaction_engine: CompactionEngine,
        history_appender: Callable[[ChatMessage], None],
        make_summary_message: Callable[..., ChatMessage],
        render_summary: Callable[[dict], str],
        merge_action_usage: Callable[[list[ChatMessage]], None] | None = None,
    ) -> None:
        self._events = event_log
        self._config = config
        self._history_access = history_access
        self._latest_summary = latest_summary
        self._engine = compaction_engine
        self._append_history = history_appender
        self._make_summary_message = make_summary_message
        self._render_summary = render_summary
        self._merge_action_usage = merge_action_usage
        self._compacting: bool = False

    # ── internal compaction logic ─────────────────────────────────────────────

    def _select_candidates(
        self,
        turns: "list[ChatMessage]",
        prev_cover: int,
    ) -> "list[ChatMessage]":
        """Select compaction candidates using token-budget-derived HEAD/TAIL boundaries.

        #1128 step 3: replaces the old seq-arithmetic on cfg.head_size/tail_size
        with token-budget trimming via the engine's ComputedBudgets.  Candidates
        are the turns strictly between the head (trim_head) and tail (trim_tail)
        slices that also have seq > prev_cover (= not yet covered by the latest
        summary).

        Falls back to a quarter of get_max_input_tokens when budgets are None
        (engine not yet initialised — highly unlikely in production but safe).
        """
        budgets = getattr(self._engine, "budgets", None)
        model = getattr(self._engine, "_model", "")
        use_chars4 = getattr(self._config, "use_chars4_estimate", False)
        if budgets is not None:
            head_budget = budgets.head_budget
            tail_budget = budgets.tail_budget
        else:
            from reyn.llm.model_budget import get_max_input_tokens
            fallback = get_max_input_tokens(model) if model else 100_000
            head_budget = tail_budget = fallback // 4

        head_turns = trim_head(turns, head_budget, model, use_chars4=use_chars4)
        tail_turns = trim_tail(turns, tail_budget, model, use_chars4=use_chars4)
        head_id_set = {id(t) for t in head_turns}
        tail_id_set = {id(t) for t in tail_turns}
        return [
            t for t in turns
            if id(t) not in head_id_set
            and id(t) not in tail_id_set
            and t.seq > prev_cover
        ]

    async def force_compact_now(self) -> None:
        """Synchronous force-trigger — single pass (#1128 PR-c).

        Used by the pre-frame guard in ``ContextBudgetAdvisor.maybe_force_compact`` when
        the projected prompt would exceed the model's max_input_tokens.  Emits
        ``compaction_check`` with ``outcome="forced_sync"``.

        #1128 PR-c: collapsed from the former Option-B race-recovery loop
        (``max_passes`` re-measure + ``ForceCompactRaceUnrecoveredError``) to a
        single pass. That loop existed to re-run when another coroutine appended
        to history mid-compaction. Cross-driver turn serialization is now
        structural — every transport that drives ``run_one_iteration`` holds the
        shared per-agent lock (PR-b, ``reyn.runtime.agent_locks``), and within a
        turn ``_append_history`` is synchronous — so no concurrent append can
        land during this method. If the single pass under-shoots (the guard's
        estimate under-counted), the ``retry_loop`` overflow backstop in
        ``_run_router_loop`` folds raw_middle and monotonically shrinks: that is
        the under-shoot floor, replacing the multi-pass-or-raise contract.

        #1128 PR-a: the former vestigial ``compaction_lock`` acquire was
        removed — only this method acquired it; no history appender awaited it.
        Cross-driver turn serialization is the shared per-agent lock's job (PR-b).
        """
        if self._compacting:
            self._events.emit("compaction_check", outcome="already_running")
            return

        history = self._history_access()
        turns = [
            m for m in history
            if m.role in ("user", "assistant", "tool", "agent")
        ]
        if not turns:
            self._events.emit("compaction_check", outcome="forced_sync_no_turns")
            return
        latest = self._latest_summary()
        prev_cover = (latest.meta or {}).get("covers_through_seq", 0) if latest else 0
        candidates = self._select_candidates(turns, prev_cover)

        self._events.emit(
            "compaction_check", outcome="forced_sync",
            candidate_count=len(candidates),
        )
        if not candidates:
            return

        self._compacting = True
        try:
            await self._run_compaction(candidates, latest)
        except Exception as exc:
            self._events.emit("compaction_failed", error=str(exc))
        finally:
            self._compacting = False

    async def _run_compaction(
        self,
        candidates: list[ChatMessage],
        previous_summary: ChatMessage | None,
    ) -> None:
        """Call the compaction engine and persist the resulting summary entry."""
        cfg = self._config
        prev_structured: dict | None = None
        if previous_summary is not None:
            meta = previous_summary.meta or {}
            structured = meta.get("structured")
            if isinstance(structured, dict):
                prev_structured = structured
                # carry forward the prior covers_through_seq for continuity
                if "covers_through_seq" not in prev_structured:
                    prev_structured = {
                        **prev_structured,
                        "covers_through_seq": meta.get("covers_through_seq", 0),
                    }

        input_chunk = HistoryChunkToCompact(
            previous_summary=prev_structured,
            new_turns=[_turn_to_compactor_input(t) for t in candidates],
            section_token_caps={
                "topic_arc": cfg.section_token_caps.topic_arc,
                "decisions": cfg.section_token_caps.decisions,
                "pending": cfg.section_token_caps.pending,
                "session_user_facts": cfg.section_token_caps.session_user_facts,
                "artifacts_referenced": cfg.section_token_caps.artifacts_referenced,
            },
        )

        new_turn_count = len(candidates)
        self._events.emit(
            "compaction_started",
            new_turn_count=new_turn_count,
            covers_through_seq=candidates[-1].seq,
            had_previous=previous_summary is not None,
        )

        chat_summary = await self._engine.compact(input_chunk)
        structured = chat_summary.to_dict()
        covers = chat_summary.covers_through_seq or candidates[-1].seq
        rendered = self._render_summary(structured)

        summary_msg = self._make_summary_message(rendered, structured, covers)
        self._append_history(summary_msg)
        # Action-usage sink (= per-agent compacted table). Fired BEFORE
        # the completed event so any downstream subscriber (TUI etc.)
        # already sees the updated hot-list state. Sink failure is
        # non-fatal — compaction itself succeeded.
        if self._merge_action_usage is not None:
            try:
                self._merge_action_usage(list(candidates))
            except Exception:
                pass
        self._events.emit(
            "compaction_completed",
            new_turn_count=new_turn_count,
            covers_through_seq=covers,
            section_lengths={
                k: len(v) if isinstance(v, list) else len(str(v))
                for k, v in structured.items()
                if k != "covers_through_seq"
            },
        )


__all__ = ["CompactionController"]
