"""_InlineRowManager — manages SkillActivityRow + ToolCallRow lifecycle.

Extracted from ConversationView (refactor tui-pr1). Holds the mutable dicts
and all methods that operate on them. ConversationView instantiates one
instance and delegates via thin wrappers, keeping the external API unchanged.

Dependencies injected via parent reference:
  parent.mount(row)           — Textual widget mounting
  parent._consume_empty_hint()— remove empty-state hint on first content
  parent.app.set_timer(...)   — deferred flush for min-display-time
  parent._write_log(text)     — write sealed row into the RichLog
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING

from .skill_activity import SkillActivityRow
from .tool_call_row import ToolCallRow

if TYPE_CHECKING:
    from .conversation import ConversationView

_TOOL_CALL_MIN_DISPLAY_S = 0.3


class _InlineRowManager:
    """Manages the lifecycle of SkillActivityRow and ToolCallRow widgets."""

    def __init__(self, parent: "ConversationView") -> None:
        self._parent = parent
        self._skill_rows: dict[str, SkillActivityRow] = {}
        self._tool_call_rows: dict[str, ToolCallRow] = {}
        self._last_failed_tool_row: ToolCallRow | None = None
        # Rows in their min-display flush window: popped from
        # ``_tool_call_rows`` but still mounted with a pending deferred-flush
        # timer. Tracked so ``clear()`` can cancel the timer + remove the row
        # (the dict sweep can't see them — stale-removal hazard, see
        # feedback_tui_deferred_timer_stale_removal_class). Each entry is
        # ``(row, timer)``; ``timer`` has a ``stop()`` method.
        self._pending_flush: list[tuple[ToolCallRow, object]] = []

    # ── skill rows ────────────────────────────────────────────────────────────

    def start_skill_row(
        self,
        run_id: str,
        skill_name: str,
        *,
        parent_run_id: str = "",
    ) -> SkillActivityRow:
        existing = self._skill_rows.get(run_id)
        if existing is not None:
            return existing
        self._parent._consume_empty_hint()
        label_prefix = ""
        if parent_run_id and parent_run_id in self._skill_rows:
            label_prefix = "  └─ "
        row = SkillActivityRow(
            run_id=run_id,
            skill_name=skill_name,
            # Key the widget id on the FULL run_id (the same value used as the
            # dedup dict key above), not ``run_id[:8]`` — that prefix is the
            # YYYYMMDD date, so two skills spawned the same day collided on one
            # widget id and the second mount raised DuplicateIds (the row was
            # then lost). Sanitise to the Textual id charset; the ``skillrow_``
            # prefix already satisfies the leading-character rule.
            id=f"skillrow_{re.sub(r'[^A-Za-z0-9_-]', '_', run_id)}",
            label_prefix=label_prefix,
        )
        self._skill_rows[run_id] = row
        self._parent.mount(row)
        return row

    def update_skill_phase(self, run_id: str, phase: str, visit: int = 1) -> None:
        row = self._skill_rows.get(run_id)
        if row is not None:
            row.set_phase(phase, visit=visit)

    def update_skill_detail(self, run_id: str, detail: str) -> None:
        row = self._skill_rows.get(run_id)
        if row is not None:
            row.set_detail(detail)

    def in_flight_skill_rows(self) -> list[SkillActivityRow]:
        return [row for row in self._skill_rows.values() if not row._finished]

    def finish_skill_row(
        self,
        run_id: str,
        *,
        success: bool = True,
        reason: str = "",
        aborted: bool = False,
    ) -> None:
        row = self._skill_rows.pop(run_id, None)
        if row is None:
            return
        row.finish(success=success, reason=reason, aborted=aborted)
        try:
            finished_text = row._build_finished()
            self._parent._write_log(finished_text)
            row.remove()
        except Exception:
            pass

    # ── tool call rows ────────────────────────────────────────────────────────

    def in_flight_tool_call_rows(self) -> list[ToolCallRow]:
        return [row for row in self._tool_call_rows.values() if not row._finished]

    def start_tool_call_row(
        self,
        op_id: str,
        tool_name: str,
        *,
        args_repr: str = "",
        parent_run_id: str = "",
    ) -> "ToolCallRow | None":
        if not op_id:
            return None
        existing = self._tool_call_rows.get(op_id)
        if existing is not None:
            return existing
        self._parent._consume_empty_hint()
        label_prefix = ""
        if parent_run_id and parent_run_id in self._skill_rows:
            label_prefix = "  └─ "
            try:
                self._skill_rows[parent_run_id].record_tool_call(
                    tool_name, args_repr,
                )
            except Exception:
                pass
        row = ToolCallRow(
            tool_name=tool_name,
            args_repr=args_repr,
            label_prefix=label_prefix,
            # Same id-collision class as the skillrow fix (#1971): the row is
            # deduped by the FULL op_id above, but ``op_id[:8]`` truncated the
            # widget id, so two distinct op_ids sharing an 8-char prefix
            # collided → DuplicateIds. Key on the full sanitized op_id.
            id=f"toolcall_{re.sub(r'[^A-Za-z0-9_-]', '_', op_id)}",
        )
        self._tool_call_rows[op_id] = row
        self._parent.mount(row)
        return row

    def complete_tool_call_row(
        self, op_id: str, *, result_snippet: str = "",
    ) -> None:
        row = self._tool_call_rows.pop(op_id, None)
        if row is None:
            return
        row.finish_success(result_snippet=result_snippet or None)
        self._flush_tool_call_row(row)

    def fail_tool_call_row(self, op_id: str, *, error: str = "") -> None:
        row = self._tool_call_rows.pop(op_id, None)
        if row is None:
            return
        row.finish_failure(reason=error)
        self._last_failed_tool_row = row
        self._flush_tool_call_row(row)

    def abort_tool_call_rows(self, reason: str = "cancelled") -> int:
        cancelled = 0
        for op_id in list(self._tool_call_rows.keys()):
            row = self._tool_call_rows.pop(op_id, None)
            if row is None:
                continue
            try:
                row.finish_aborted(reason=reason)
                self._flush_tool_call_row(row)
                cancelled += 1
            except Exception:
                pass
        return cancelled

    def latest_failed_tool_row(self) -> "ToolCallRow | None":
        return self._last_failed_tool_row

    def _flush_tool_call_row(self, row: ToolCallRow) -> None:
        elapsed = row.mounted_for_seconds()
        if elapsed < _TOOL_CALL_MIN_DISPLAY_S:
            delay = _TOOL_CALL_MIN_DISPLAY_S - elapsed
            try:
                timer = self._parent.app.set_timer(
                    delay, lambda: self._do_flush_tool_call_row(row),
                )
                # Track so clear() can cancel + remove this still-mounted row.
                self._pending_flush.append((row, timer))
                return
            except Exception:
                pass
        self._do_flush_tool_call_row(row)

    def _do_flush_tool_call_row(self, row: ToolCallRow) -> None:
        # The deferred flush fired (or we flushed immediately) — drop any
        # pending-flush tracking for this row so clear() doesn't double-handle.
        if self._pending_flush:
            self._pending_flush = [
                (r, t) for (r, t) in self._pending_flush if r is not row
            ]
        try:
            line1 = row._build_line1()
            line2 = row._build_line2()
            self._parent._write_log(line1)
            if line2.plain:
                self._parent._write_log(line2)
            row.remove()
        except Exception:
            pass

    # ── clear ─────────────────────────────────────────────────────────────────

    def clear(self) -> None:
        """Reset all inline row state; called from ConversationView.clear()."""
        for row in list(self._skill_rows.values()):
            row.finish(success=True, reason="cleared")
        self._skill_rows.clear()
        for row in list(self._tool_call_rows.values()):
            try:
                row.finish_aborted("cleared")
                row.remove()
            except Exception:
                pass
        self._tool_call_rows.clear()
        # Rows mid-flush are popped from the dict above but still mounted with a
        # pending timer — invisible to the sweep, the same untracked hazard the
        # InterventionWidget query-sweep in ConversationView.clear() handles.
        # Cancel the timer (so it can't write stale lines into the cleared log)
        # and remove the ghost row.
        for row, timer in self._pending_flush:
            try:
                timer.stop()
            except Exception:
                pass
            try:
                row.remove()
            except Exception:
                pass
        self._pending_flush.clear()
        self._last_failed_tool_row = None
