"""CompactionController — background head/body/tail compaction.

Extracted from ChatSession (FP-0019 Wave 1).  Owns the background
asyncio.Task that drives OS-internal compaction (PR-N3: direct Python
helper, no skill/phase overhead).

All event emissions go through the injected ``event_log``; no silent
state changes (P6).  Business logic lives entirely here; ChatSession
delegates via :meth:`spawn_maybe`, :meth:`cancel`, and
:meth:`force_compact_now` (P3).

Axis 8 (B_M trigger race strict):
    ``force_compact_now()`` acquires the engine's ``compaction_lock``
    (asyncio.Lock) for the entire duration of the compaction run.
    Any code path that appends to ``ChatSession.history`` between the
    force-trigger decision and compaction completion must await this
    lock before appending, ensuring the mathematical gap is 0: no new
    turn can land between the "T(M) > effective_trigger" decision and
    the moment compaction reduces T(M) back below the budget.
"""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Callable

from reyn.config import CompactionConfig
from reyn.events.events import EventLog
from reyn.services.compaction.engine import (
    CompactionEngine,
    HistoryChunkToCompact,
)

if TYPE_CHECKING:
    from reyn.chat.session import ChatMessage

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
        Session-scoped :class:`~reyn.events.events.EventLog`.  All
        compaction events are emitted here.
    config:
        :class:`~reyn.config.CompactionConfig` — thresholds and sizing.
    history_access:
        Zero-argument callable that returns a read-only snapshot of the
        current chat history (``list[ChatMessage]``).
    latest_summary:
        Zero-argument callable that returns the most recent ``"summary"``
        :class:`~reyn.chat.session.ChatMessage`, or ``None``.
    compaction_engine:
        :class:`~reyn.services.compaction.engine.CompactionEngine`
        that owns the single LLM call (PR-N3: OS-internal, no skill/phase).
    history_appender:
        Callable ``(ChatMessage) -> None`` that appends a message to the
        persisted history.  Wraps ``ChatSession._append_history``.
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
        self._task: asyncio.Task | None = None

    # ── public API ────────────────────────────────────────────────────────────

    def spawn_maybe(self) -> None:
        """Fire-and-forget :meth:`_maybe_compact` in a background task.

        Replaces the ``asyncio.create_task(self._maybe_compact())`` call that
        previously lived at session.py L1009.  The caller MUST NOT ``await``
        this method — it returns immediately.
        """
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._maybe_compact())

    async def cancel(self) -> None:
        """Graceful shutdown — cancel in-flight task and suppress CancelledError.

        Replaces the shutdown block at session.py L943-948.  Any non-cancellation
        exception is logged and a ``compaction_failed`` event is emitted.
        """
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                logger.warning("compaction task failed during shutdown: %s", exc)
                self._events.emit(
                    "compaction_failed", error=str(exc), phase="shutdown"
                )

    # ── internal compaction logic ─────────────────────────────────────────────

    async def _maybe_compact(self) -> None:
        """Threshold judgement + compaction trigger.

        Originally session.py L1313-1368.

        Trigger: estimated tokens of user/agent turns whose seq is BOTH:
        - > head_size (those are HEAD, never compacted)
        - > latest_summary.covers_through_seq (already covered)
        - <= max_seq - tail_size (TAIL is preserved as raw)
        exceeds ``config.trigger_total_tokens`` and the candidate set
        contains at least ``config.min_compact_batch`` turns.
        """
        if self._compacting:
            self._events.emit("compaction_check", outcome="already_running")
            return
        cfg = self._config
        history = self._history_access()
        # E-full PR-E2 (#383): tool turns (= role="assistant" with
        # tool_calls + role="tool" responses) are now first-class history
        # entries and should be compactable. "agent" is the pre-#383
        # spelling of "assistant" — kept here for backward-compat with
        # any legacy entries that escaped read-time migration.
        turns = [
            m for m in history
            if m.role in ("user", "assistant", "tool", "agent")
        ]
        if len(turns) <= cfg.head_size + cfg.tail_size:
            self._events.emit(
                "compaction_check", outcome="too_few_turns",
                turns=len(turns), head=cfg.head_size, tail=cfg.tail_size,
            )
            return

        latest = self._latest_summary()
        prev_cover = (latest.meta or {}).get("covers_through_seq", 0) if latest else 0
        cover_floor = max(prev_cover, cfg.head_size)

        max_seq = max((t.seq for t in turns), default=0)
        tail_threshold = max_seq - cfg.tail_size
        candidates = [t for t in turns if cover_floor < t.seq <= tail_threshold]
        if len(candidates) < cfg.min_compact_batch:
            self._events.emit(
                "compaction_check", outcome="below_min_batch",
                candidate_count=len(candidates), min_batch=cfg.min_compact_batch,
            )
            return

        total_tokens = sum(_estimate_tokens(t.text) for t in candidates)
        if total_tokens < cfg.trigger_total_tokens:
            self._events.emit(
                "compaction_check", outcome="below_threshold",
                total_tokens=total_tokens, threshold=cfg.trigger_total_tokens,
                candidate_count=len(candidates),
            )
            return
        self._events.emit(
            "compaction_check", outcome="triggering",
            total_tokens=total_tokens, candidate_count=len(candidates),
        )

        self._compacting = True
        try:
            await self._run_compaction(candidates, latest)
        except Exception as exc:
            self._events.emit("compaction_failed", error=str(exc))
        finally:
            self._compacting = False

    def _estimate_current_history_tokens(self) -> int:
        """Cheap chars/4 estimate of the total text tokens in current history.

        Used by the race-recovery loop in :meth:`force_compact_now` to
        re-measure after each compaction pass.
        """
        history = self._history_access()
        return sum(
            _estimate_tokens(m.text)
            for m in history
            if m.role in ("user", "assistant", "tool", "agent")
        )

    async def force_compact_now(self, *, max_passes: int = 2) -> None:
        """Synchronous force-trigger with race-recovery loop (ISSUE #6, Option B).

        Used by the pre-frame guard in ``_maybe_force_compact_for_router`` when
        the projected prompt would exceed the model's max_input_tokens.  Emits
        the same events as the background path but with ``outcome="forced_sync"``
        on the ``compaction_check`` event.

        Race tolerance (Option B):
            Between the fire-decision and lock-acquisition, other async
            coroutines may append to history (sync ``_append_history`` calls).
            After each compaction pass, we re-measure the post-state and re-run
            if still over budget, up to ``max_passes`` total.  Beyond that, a
            ``force_compact_race_unrecovered`` event is emitted and
            ``ForceCompactRaceUnrecoveredError`` is raised — the contract is
            fail-fast (lead-coder accept condition 2026-05-29): the caller
            must surface the unrecovered state rather than allow a silent
            over-budget LLM call.

            Choice of Option B over Option A (= async _append_history + lock):
            Option B is more localised (no call-site changes to _append_history),
            matches Python async semantics where sync appends are sequential
            within a coroutine, and the worst-case is a single over-budget turn
            that resolves on the very next interaction cycle. Pairing the
            ``force_compact_race_unrecovered`` event with a raise preserves the
            mathematical invariant: either compaction succeeds within
            max_passes, or the caller sees a hard error — never a silent
            over-budget prompt.

        The existing background ``spawn_maybe()`` fire-and-forget path is
        unaffected; both paths can co-exist (sync = pre-frame guard,
        async = post-reply background trigger).

        Axis 8: acquires the engine's ``compaction_lock`` for the duration of
        each run.  Any concurrent code path that appends to ChatSession.history
        and needs to serialise with compaction must await the same lock before
        appending.
        """
        if self._compacting:
            self._events.emit("compaction_check", outcome="already_running")
            return
        cfg = self._config

        for pass_n in range(max_passes):
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
            cover_floor = max(prev_cover, cfg.head_size)
            max_seq = max((t.seq for t in turns), default=0)
            tail_threshold = max_seq - cfg.tail_size
            candidates = [t for t in turns if cover_floor < t.seq <= tail_threshold]

            self._events.emit(
                "compaction_check", outcome="forced_sync",
                candidate_count=len(candidates),
                pass_n=pass_n,
            )
            if not candidates:
                return

            self._compacting = True
            async with self._engine.compaction_lock:
                try:
                    await self._run_compaction(candidates, latest)
                except Exception as exc:
                    self._events.emit("compaction_failed", error=str(exc))
                    return
                finally:
                    self._compacting = False

            # Re-measure post-compaction. If still over effective_trigger, loop.
            budgets = getattr(self._engine, "budgets", None)
            effective_trigger = (
                budgets.effective_trigger if budgets is not None else 0
            )
            if effective_trigger > 0:
                post_tokens = self._estimate_current_history_tokens()
                if post_tokens <= effective_trigger:
                    return  # budget recovered — done
            else:
                return  # no trigger info — assume done

        # All passes exhausted; race not fully recovered.
        # Fail-fast per lead-coder accept condition 2026-05-29: the contract
        # is "compaction succeeds within max_passes OR raises". Silent-continue
        # would allow an over-budget prompt to reach the LLM.
        from reyn.services.compaction.engine import (
            ForceCompactRaceUnrecoveredError,
        )
        self._events.emit(
            "force_compact_race_unrecovered",
            passes=max_passes,
        )
        raise ForceCompactRaceUnrecoveredError(passes=max_passes)

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
