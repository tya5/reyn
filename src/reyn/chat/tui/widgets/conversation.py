"""ConversationView — scrollable conversation pane.

Composition (top → bottom):
  - RichLog (1fr) — the main append-only log of user/agent messages
  - Per-stream / inline widgets mounted as children: StreamingRow, ErrorBox,
    SkillActivityRow, InterventionWidget — all height:auto so they stack
    naturally below the log as it streams.
  - StickyStatus (dock: bottom, h:1) — pins at the very bottom; replaces
    inline `⟳ thinking…` log lines.

Message-kind routing:
  agent       → header (timestamp + label, optionally suppressed when
                consecutive turns are within _GROUP_WINDOW_S) followed
                by FoldableMarkdown so long replies can be folded.
  status      → routed to StickyStatus (sticky 1-line, never logged).
  error       → mounted as an ErrorBox widget (collapsible 1-line).
  intervention→ InterventionWidget (mount_intervention).
  trace       → suppressed in the conv pane; the App's outbox loop drives
                a SkillActivityRow instead. Right panel events tab still
                shows the full picture.
  skill_done  → suppressed; SkillActivityRow.finish() handles the visible
                completion line.

Empty state:
  The pane mounts a single dim hint ("Type / for commands · …") that auto-
  removes on the first user/agent message.

Turn navigation (B4):
  ConversationView records the RichLog line index at the start of every
  agent header. ReynTUIApp's Ctrl+P / Ctrl+N actions call jump_prev_turn /
  jump_next_turn to scroll the log to the previous/next turn anchor.

Cost suffix (A4):
  When the App enables cost-inline mode, _render_agent_cost_suffix() is
  called after a turn ends and writes a dim "⌁ Δ tokens · $0.XXXX" line.
"""
from __future__ import annotations

import time

from rich.cells import cell_len
from rich.markdown import Markdown as RichMarkdown
from rich.text import Text
from textual.app import ComposeResult
from textual.widget import Widget
from textual.widgets import RichLog, Static

from reyn.chat.outbox import OutboxMessage
from reyn.chat.tui._palette import _CORAL

from .error_box import ErrorBox
from .intervention import InterventionWidget
from .skill_activity import SkillActivityRow
from .sticky_status import StickyStatus
from .streaming_row import StreamingRow

_DASH_TOTAL = 38  # matches the banner separator width
_GROUP_WINDOW_S = 60.0  # consecutive turns within this window share a header
_FOLD_THRESHOLD_LINES = 30  # B3: agent replies above this fold inline
# RichLog ring-buffer size. Bumped from the historical 5000 → 20000 to push
# the truncation boundary well past realistic session lengths (~400-800
# turns at typical reply sizes). Storage stays modest at average line width;
# turn anchors below are drop-aware so even pathological sessions that DO
# cross the boundary don't break Ctrl+P/N navigation.
_RICHLOG_MAX_LINES = 20_000
_NAME_COL_COLS = 4  # display-cell width reserved for the speaker label column


def _pad_to_cells(s: str, target_cells: int) -> str:
    """Right-pad ``s`` with spaces so its terminal column width >= target.

    ``str.ljust`` counts code points, but terminal columns count display
    cells — a CJK character (or full-width punctuation, or an emoji)
    occupies 2 columns per glyph. Using ``ljust(4)`` on an agent name
    like ``"アリア"`` leaves it at 6 cells when the next column expects
    a fixed 4-cell offset, breaking the dash-rule alignment between
    user (``"you "``) and agent headers.

    Returns ``s`` unchanged when it already meets or exceeds the target;
    truncation would split a wide glyph and is left to the caller.
    """
    width = cell_len(s)
    if width >= target_cells:
        return s
    return s + " " * (target_cells - width)


def _msg_header(label: str, name_style: str, dash_style: str) -> Text:
    """Timestamp + label + dash rule for a new message turn.

    Column layout: ``HH:MM`` (5) + 2 spaces + label (>=4 cells) + 1 space +
    dashes. The dash count flexes with the actual cell width of ``label``
    so wide-character agent names don't push the line past _DASH_TOTAL.
    """
    t = Text()
    t.append(time.strftime("%H:%M"), style="dim #666666")
    t.append("  ")
    padded = _pad_to_cells(label, _NAME_COL_COLS)
    name_cells = cell_len(padded)
    t.append(padded, style=name_style)
    t.append(" ")
    dashes = max(1, _DASH_TOTAL - 5 - 2 - name_cells - 1)
    t.append("─" * dashes, style=dash_style)
    return t


def _meta_prefix(meta: dict) -> str:
    """Build [skill#abcd] prefix from meta, same logic as renderer.py."""
    skill = meta.get("skill_name")
    short = meta.get("run_id_short")
    if skill and short:
        return f"[{skill}#{short}] "
    if skill:
        return f"[{skill}] "
    if short:
        return f"[#{short}] "
    return ""


class ConversationView(Widget):
    """Main conversation pane: RichLog + inline widgets + sticky status.

    Streaming:
      - `begin_stream(msg_id, agent_name)` → mounts a StreamingRow.
      - `append_stream(msg_id, text)` → calls StreamingRow.append().
      - `end_stream(msg_id)` → seals the row.

    Skill activity (replaces trace lines):
      - `start_skill_row(run_id, skill_name)` → mounts a SkillActivityRow.
      - `update_skill_phase(run_id, phase, visit)` → set_phase on the row.
      - `finish_skill_row(run_id, success, reason)` → finish the row.

    Sticky status:
      - `show_status(text, kind)` / `hide_status()` → drive StickyStatus.

    Errors:
      - `mount_error(message, details, ...)` → ErrorBox widget.
    """

    DEFAULT_CSS = """
    ConversationView {
        height: 1fr;
        padding: 0 0;
    }
    ConversationView RichLog {
        background: transparent;
        height: 1fr;
        border: none;
        /* Dim by default; only the active/hover scrollbar uses the coral
           highlight. Avoids the full-track coral block when content fits
           in one viewport. */
        scrollbar-color: #2a2a2a;
        scrollbar-color-hover: $primary;
        scrollbar-color-active: $primary;
        scrollbar-background: transparent;
        scrollbar-background-hover: transparent;
        scrollbar-background-active: transparent;
        scrollbar-corner-color: transparent;
        scrollbar-size-vertical: 1;
        scrollbar-size-horizontal: 1;
        padding: 0 1;
    }
    ConversationView #empty-hint {
        color: #555555;
        padding: 0 1;
        height: auto;
    }
    ConversationView #empty-hint.hidden {
        display: none;
    }
    """

    def __init__(self, *, scroll_end: bool = True, id: str | None = None) -> None:
        super().__init__(id=id)
        self._scroll_end = scroll_end
        self._stream_rows: dict[str, StreamingRow] = {}
        self._skill_rows: dict[str, SkillActivityRow] = {}
        # Header-grouping state (B1)
        self._last_speaker: str = ""
        self._last_speaker_at: float = 0.0
        # Empty-state (B5)
        self._has_first_message = False
        # Turn navigation (B4) — absolute line positions for each turn header.
        # "Absolute" = ``log._start_line + len(log.lines)`` at write time, NOT
        # the bare ``len(log.lines)`` value. RichLog uses a ring buffer; once
        # the session crosses _RICHLOG_MAX_LINES, ``log._start_line`` grows
        # and ``log.lines`` shifts. Bare-index anchors silently rot the moment
        # the first line is dropped; absolute positions stay stable and we
        # convert back to the current ``log.lines`` index on read.
        self._turn_anchors: list[int] = []
        # One-shot flag so the "earlier history trimmed" warning fires at most
        # once per session (= the first time Ctrl+P/N is used after trim).
        self._trim_warned = False
        # B3 — full text of the last truncated agent reply (or None when the
        # most recent reply fit within _FOLD_THRESHOLD_LINES).
        self._last_long_reply: str | None = None
        # Full text of the most recent agent reply (any length). Consumed by
        # the /copy slash command so users don't have to fight the TUI's
        # mouse-capture to grab text out of the log.
        self._last_reply_full: str | None = None
        # Track whether user has scrolled up (suppress auto-scroll while scrolled)
        self._user_scrolled = False
        # Issue 5 — track mounted ErrorBoxes for Escape-to-dismiss
        self._error_boxes: list[ErrorBox] = []

    # ── composition ──────────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        # ``can_focus = False`` is the load-bearing piece: RichLog inherits
        # ``can_focus = True`` from ScrollView, so a stray click on the conv
        # pane (or Shift+Tab from the input bar) silently shifts focus here.
        # The log is append-only and accepts no typed input, so the user then
        # types into nothing until they click the input bar again. Ctrl+P/N
        # turn-nav already calls ``log.scroll_to`` without needing focus, so
        # disabling focus loses no real capability.
        log = RichLog(highlight=False, markup=False, wrap=True,
                      max_lines=_RICHLOG_MAX_LINES, id="log")
        log.can_focus = False
        yield log
        # B5: empty-state hint, removed on first message.
        # The hint is the first thing a new user sees, so it has to do double
        # duty: announce the core key-binds AND describe the side panel — the
        # most sophisticated piece of the TUI, which is hidden by default and
        # otherwise has zero discoverability on first run.
        yield Static(
            "  Type [bold]/[/] for commands  ·  [bold]/help[/] for a guide\n"
            "  [bold]Ctrl+B[/] side panel ([dim]keys · events · agents · memory · cost · docs[/])  ·  "
            "[bold]Ctrl+L[/] clear  ·  [bold]Ctrl+P/N[/] turn",
            id="empty-hint",
            markup=True,
        )
        # A3: sticky status pinned to bottom of conv pane
        yield StickyStatus(id="sticky-status")

    def _log(self) -> RichLog:
        return self.query_one("#log", RichLog)

    def _sticky(self) -> StickyStatus | None:
        try:
            return self.query_one("#sticky-status", StickyStatus)
        except Exception:
            return None

    # ── empty state ───────────────────────────────────────────────────────────

    def _consume_empty_hint(self) -> None:
        if self._has_first_message:
            return
        self._has_first_message = True
        try:
            # Hide instead of remove so /clear can bring it back.
            self.query_one("#empty-hint", Static).add_class("hidden")
        except Exception:
            pass

    # ── header grouping (B1) ──────────────────────────────────────────────────

    def _maybe_write_header(self, speaker: str, label_text: str,
                             name_style: str, dash_style: str) -> None:
        """Write a header line only when the speaker changes or the gap is
        larger than _GROUP_WINDOW_S. Stores the new state."""
        now = time.monotonic()
        same_speaker = (speaker == self._last_speaker)
        within_window = (now - self._last_speaker_at) < _GROUP_WINDOW_S
        if not (same_speaker and within_window):
            log = self._log()
            # Record a turn anchor whenever a new speaker header appears
            # — agent replies, user inputs, and system / slash-command
            # results. Ctrl+P / Ctrl+N then walk through every header in
            # order, which matches the user's mental model of "jump to
            # the previous turn" better than "jump only to agent replies".
            self._turn_anchors.append(self._absolute_line_position(log))
            # Cap to last 200 to avoid unbounded growth (long sessions)
            if len(self._turn_anchors) > 200:
                self._turn_anchors = self._turn_anchors[-200:]
            log.write(_msg_header(label_text, name_style, dash_style))
        self._last_speaker = speaker
        self._last_speaker_at = now

    # ── non-streaming message rendering ───────────────────────────────────────

    def render_message(self, msg: OutboxMessage) -> None:
        """Append an OutboxMessage to the log (or route via dedicated widget).

        Routing:
          agent      → header + Markdown inline (with B3 fold for >30 lines)
          system     → header + plain-text inline (persistent slash output)
          intervention/trace/status/skill_done → suppressed (handled elsewhere)
          error      → ErrorBox widget
          others     → plain Rich Text line
        """
        if msg.kind == "intervention":
            # Interventions are handled via mount_intervention, not here.
            self._consume_empty_hint()
            self._write_log(_format_intervention_line(msg))
            return

        if msg.kind in {"trace", "status", "skill_done"}:
            # Suppressed — these are handled by:
            #   trace       → start/update_skill_row (driven from app.py)
            #   status      → show_status (sticky)
            #   skill_done  → finish_skill_row (driven from app.py)
            return

        if msg.kind == "agent":
            self._render_agent_markdown(msg)
            return

        if msg.kind == "system":
            self._render_system_message(msg)
            return

        if msg.kind == "error":
            self.mount_error(
                message=msg.text,
                details=str(msg.meta.get("details", "")),
                run_id_short=str(msg.meta.get("run_id_short", "")),
                skill_name=str(msg.meta.get("skill_name", "")),
            )
            return

        text = _format_message(msg)
        if text is not None:
            self._consume_empty_hint()
            self._write_log(text)

    def render_user_message(self, text: str) -> None:
        """Render a freshly submitted user message with grouped header."""
        self._consume_empty_hint()
        self._maybe_write_header("you", "you", "bold #4abbb5", "#1f5856")
        self._write_log(Text(text))
        self._write_log(Text(""))

    def _render_system_message(self, msg: OutboxMessage) -> None:
        """Render a slash-command (or other OS-generated) message persistently.

        Distinct from ``agent`` so the log doesn't claim the LLM produced
        these lines, and distinct from ``status`` so prior outputs survive
        when running multiple commands in a row.

        Rendered as plain text (newlines preserved, no Markdown) under a
        neutral ``system`` header in dim grey.
        """
        self._consume_empty_hint()
        self.hide_status()
        self._maybe_write_header("system", "system", "bold #888888", "#444444")
        log = self._log()
        for line in (msg.text or "").splitlines() or [""]:
            log.write(Text(line))
        self._write_log(Text(""))

    def _render_agent_markdown(self, msg: OutboxMessage) -> None:
        """Render a non-streaming agent message inline in the log.

        Writes Markdown directly into the RichLog (as a Rich renderable) so
        agent replies appear under their header instead of being pushed to
        the bottom of the pane. Hides any sticky "thinking…" indicator that
        was active for this turn.
        """
        self._consume_empty_hint()
        self.hide_status()  # turn finished — clear "thinking…" sticky
        meta_pfx = _meta_prefix(msg.meta)
        label = f"reyn  {meta_pfx}".rstrip() if meta_pfx else "reyn"
        self._maybe_write_header("reyn", label, "bold " + _CORAL, "#5a2020")
        if msg.text:
            self._write_agent_markdown_with_fold(msg.text)
        self._write_log(Text(""))

    # ── B3 fold (long-reply truncation + /expand) ────────────────────────────

    def _write_agent_markdown_with_fold(self, text: str) -> None:
        """Write `text` as Markdown into the log; truncate when it's too long.

        Long replies (> _FOLD_THRESHOLD_LINES) are truncated and a dim hint is
        appended pointing the user at /expand. The full text is stashed in
        self._last_long_reply so expand_last_reply() can flush the rest.
        Replies that fit are rendered as-is and clear any pending fold.

        Side effect: stores ``text`` in ``self._last_reply_full`` so the
        /copy slash command can hand it to the system clipboard (no need
        to fight TUI mouse-capture for selection).
        """
        # Always remember the full text — independent of fold thresholds.
        self._last_reply_full = text
        log = self._log()
        # Detect that a prior fold is about to be invalidated. The single-slot
        # ``_last_long_reply`` is replaced (or cleared) by every new agent
        # reply, so any old fold hint up-screen still reads "type /expand to
        # show" while /expand itself silently no-ops. Flag it inline so the
        # user can tell the previous fold is no longer reachable.
        had_prev_fold = self._last_long_reply is not None
        lines = text.split("\n")
        if len(lines) <= _FOLD_THRESHOLD_LINES:
            log.write(RichMarkdown(text))
            if had_prev_fold:
                self._write_fold_expired_marker()
            self._last_long_reply = None
            return
        preview = "\n".join(lines[:_FOLD_THRESHOLD_LINES])
        remaining = len(lines) - _FOLD_THRESHOLD_LINES
        log.write(RichMarkdown(preview))
        hint = Text()
        hint.append(
            f"  [ … {remaining} more lines · type ",
            style=f"dim {_CORAL}",
        )
        hint.append("/expand", style=f"bold {_CORAL}")
        hint.append(" to show ]", style=f"dim {_CORAL}")
        log.write(hint)
        if had_prev_fold:
            self._write_fold_expired_marker()
        self._last_long_reply = text

    def _write_fold_expired_marker(self) -> None:
        """Emit a dim marker noting that an earlier fold's /expand is gone.

        The fold stash is a single slot — every new agent reply either
        clears it (short reply) or replaces it (next long reply). Either
        way the earlier fold's /expand becomes unreachable; this marker
        makes that visible chronologically so users don't keep typing
        /expand into a no-op.
        """
        marker = Text()
        marker.append("  [ ↑ earlier fold cleared ]", style=f"dim {_CORAL}")
        self._log().write(marker)

    def expand_last_reply(self) -> bool:
        """Append the full text of the most recently truncated reply.

        Returns True when a reply was expanded; False when nothing pending.
        After expanding, the stash is cleared (only one expand per fold).
        """
        if not self._last_long_reply:
            return False
        log = self._log()
        marker = Text()
        marker.append("  ↓ expanded", style=f"dim {_CORAL}")
        log.write(marker)
        log.write(RichMarkdown(self._last_long_reply))
        log.write(Text(""))
        self._last_long_reply = None
        return True

    @property
    def has_pending_expand(self) -> bool:
        return self._last_long_reply is not None

    def last_reply_text(self) -> str | None:
        """Return the full text of the most recent agent reply (any length).

        Used by the /copy slash command. Returns None when there has been no
        agent reply in this session yet.
        """
        return self._last_reply_full

    def _write_log(self, text: Text) -> None:
        log = self._log()
        log.write(text)

    # ── streaming support ─────────────────────────────────────────────────────

    def begin_stream(self, msg_id: str, agent_name: str = "") -> StreamingRow:
        """Start a streaming agent message row. Returns the row widget."""
        self._consume_empty_hint()
        label = agent_name if agent_name else "reyn"
        self._maybe_write_header("reyn", label, "bold " + _CORAL, "#5a2020")
        row = StreamingRow(prefix="", id=f"stream_{msg_id[:8]}")
        self._stream_rows[msg_id] = row
        self.mount(row)
        return row

    def append_stream(self, msg_id: str, text: str) -> None:
        row = self._stream_rows.get(msg_id)
        if row is not None:
            row.append(text)

    def end_stream(self, msg_id: str) -> str:
        """Seal the stream and flush the final content INTO the RichLog inline,
        then remove the transient StreamingRow widget so the bottom of the
        pane stays empty (or holds the next streaming row).

        Long replies are truncated with a /expand hint (B3 fold).
        """
        row = self._stream_rows.pop(msg_id, None)
        if row is None:
            return ""
        full = row.full_text()
        # Seal stops the cursor + 16ms tick.
        row.seal()
        self.hide_status()
        if full:
            try:
                self._write_agent_markdown_with_fold(full)
            except Exception:
                self._log().write(Text(full))
        self._log().write(Text(""))
        try:
            row.remove()
        except Exception:
            pass
        return full

    # ── skill activity rows (C1+A1) ──────────────────────────────────────────

    def start_skill_row(self, run_id: str, skill_name: str) -> SkillActivityRow:
        """Mount (or return existing) SkillActivityRow for a skill run.

        Suppresses the noisy `· phase started: …` trace stream by giving it
        a single ambient widget that updates in-place.
        """
        existing = self._skill_rows.get(run_id)
        if existing is not None:
            return existing
        self._consume_empty_hint()
        row = SkillActivityRow(
            run_id=run_id,
            skill_name=skill_name,
            id=f"skillrow_{run_id[:8]}",
        )
        self._skill_rows[run_id] = row
        self.mount(row)
        return row

    def update_skill_phase(self, run_id: str, phase: str, visit: int = 1) -> None:
        row = self._skill_rows.get(run_id)
        if row is not None:
            row.set_phase(phase, visit=visit)

    def finish_skill_row(
        self, run_id: str, *, success: bool = True, reason: str = "",
    ) -> None:
        row = self._skill_rows.pop(run_id, None)
        if row is not None:
            row.finish(success=success, reason=reason)

    # ── sticky status (A3) ────────────────────────────────────────────────────

    def show_status(self, text: str, kind: str = "thinking") -> None:
        s = self._sticky()
        if s is not None:
            s.show(text, kind=kind)

    def update_status(self, text: str) -> None:
        s = self._sticky()
        if s is not None:
            s.update_text(text)

    def hide_status(self) -> None:
        s = self._sticky()
        if s is not None:
            s.hide()

    # ── error box (A2) ────────────────────────────────────────────────────────

    def mount_error(
        self,
        *,
        message: str,
        details: str = "",
        run_id_short: str = "",
        skill_name: str = "",
    ) -> ErrorBox:
        self._consume_empty_hint()
        # Hide any sticky "thinking…" — the turn is over (it failed).
        # Otherwise the elapsed counter keeps incrementing forever next
        # to a stale message, e.g. "⟳ thinking · 87.4s" while the
        # ErrorBox below already shows "router failed".
        self.hide_status()
        box = ErrorBox(
            message=message,
            details=details,
            run_id_short=run_id_short,
            skill_name=skill_name,
        )
        self.mount(box)
        self._error_boxes.append(box)
        try:
            box.scroll_visible()
        except Exception:
            pass
        return box

    def has_error_boxes(self) -> bool:
        """Return True if any undismissed ErrorBox remains."""
        return bool(self._error_boxes)

    def dismiss_last_error(self) -> None:
        """Remove the most recently mounted ErrorBox (idempotent if already removed)."""
        while self._error_boxes:
            box = self._error_boxes.pop()
            try:
                box.remove()
                return
            except Exception:
                continue  # already removed, try next

    # ── intervention mounting ─────────────────────────────────────────────────

    def mount_intervention(
        self,
        *,
        question: str,
        choices: list[tuple[str, str] | dict] | None = None,
        answer_callback=None,
        iv_id: str = "",
        queued_extra: int = 0,
    ) -> InterventionWidget:
        self._consume_empty_hint()
        # The run is now blocked on the user's answer; "thinking…" is no
        # longer accurate, hide the live counter while we wait.
        self.hide_status()
        widget = InterventionWidget(
            question=question,
            choices=choices,
            answer_callback=answer_callback,
            iv_id=iv_id,
            queued_extra=queued_extra,
        )
        self.mount(widget)
        widget.scroll_visible()
        return widget

    # ── cost suffix (A4) ──────────────────────────────────────────────────────

    def render_cost_suffix(self, tokens: int, cost_usd: float, elapsed_s: float) -> None:
        """Append a dim per-turn cost suffix, right-aligned. Caller decides when (opt-in).

        Both pieces are load-bearing: ``Text(..., justify="right")`` only
        right-aligns when the renderer is told a width to fill, and
        ``RichLog.write`` defaults to ``expand=False`` — without
        ``expand=True`` the suffix silently renders at column 0.
        """
        t = Text(
            f"⌁ {tokens}t · ${cost_usd:.4f} · {elapsed_s:.1f}s",
            style="dim #666666",
            justify="right",
        )
        self._log().write(t, expand=True)

    # ── turn navigation (B4) ──────────────────────────────────────────────────

    def jump_prev_turn(self) -> None:
        """Scroll the log to the previous agent turn anchor."""
        self._jump_to_relative_anchor(-1)

    def jump_next_turn(self) -> None:
        """Scroll the log to the next agent turn anchor."""
        self._jump_to_relative_anchor(+1)

    @staticmethod
    def _absolute_line_position(log: RichLog) -> int:
        """Return the absolute write-position (drop-aware) for the next line.

        ``log._start_line`` is RichLog's cumulative dropped-lines counter
        (private but stable). Combined with ``len(log.lines)``, it yields
        a monotonic absolute index that survives the ring-buffer trim —
        unlike the bare ``len(log.lines)`` value, which silently rebases
        the moment ``max_lines`` is exceeded.
        """
        return getattr(log, "_start_line", 0) + len(log.lines)

    def _resolve_anchors_to_current_view(self, log: RichLog) -> list[int]:
        """Project stored absolute anchors back into current ``log.lines`` indexes.

        Anchors whose target line has been trimmed (= ``absolute - start < 0``)
        are silently dropped: jumping to a turn that no longer exists in the
        log would scroll to whatever line happens to occupy that slot now,
        which is exactly the bug the bare-index version exhibited.
        """
        start = getattr(log, "_start_line", 0)
        return [a - start for a in self._turn_anchors if a - start >= 0]

    def _maybe_warn_about_trimmed_history(self, log: RichLog) -> None:
        """Surface a one-shot dim status when older history has been trimmed.

        We only fire once per session — repeated Ctrl+P presses past the
        top would otherwise spam the sticky status with the same message.
        ``/clear`` resets the flag so a fresh session can warn again.
        """
        if self._trim_warned:
            return
        start = getattr(log, "_start_line", 0)
        if start <= 0:
            return
        self._trim_warned = True
        sticky = self._sticky()
        if sticky is not None:
            try:
                sticky.show(
                    f"↑ earlier history trimmed ({start:,} lines)",
                    kind="general",
                )
            except Exception:
                pass

    def _jump_to_relative_anchor(self, delta: int) -> None:
        if not self._turn_anchors:
            return
        log = self._log()
        anchors = self._resolve_anchors_to_current_view(log)
        if not anchors:
            self._maybe_warn_about_trimmed_history(log)
            return
        # If the trim swallowed some anchors, surface that to the user.
        if len(anchors) < len(self._turn_anchors):
            self._maybe_warn_about_trimmed_history(log)
        # Find the nearest anchor >= or <= current scroll y
        cur_y = log.scroll_y
        if delta < 0:
            target = None
            for a in reversed(anchors):
                if a < cur_y - 1:  # strictly above current view
                    target = a
                    break
            if target is None:
                target = anchors[0]
        else:
            target = None
            for a in anchors:
                if a > cur_y + 1:  # strictly below current view
                    target = a
                    break
            if target is None:
                target = anchors[-1]
        try:
            log.scroll_to(y=target, animate=False)
        except Exception:
            pass

    def clear(self) -> None:
        """Ctrl+L: clear the log + reset state. Does not affect engine state."""
        self._log().clear()
        for row in self._stream_rows.values():
            row.seal()
        self._stream_rows.clear()
        # Force-finish any in-progress skill rows so they don't keep ticking
        for row in list(self._skill_rows.values()):
            row.finish(success=True, reason="cleared")
        self._skill_rows.clear()
        # Reset header-grouping + turn anchors + fold stash
        self._last_speaker = ""
        self._last_speaker_at = 0.0
        self._turn_anchors.clear()
        self._trim_warned = False
        self._last_long_reply = None
        # Hide sticky status
        self.hide_status()
        # Restore the empty-state hint so the next session looks fresh.
        self._has_first_message = False
        try:
            self.query_one("#empty-hint", Static).remove_class("hidden")
        except Exception:
            pass


# ── formatting helpers ─────────────────────────────────────────────────────────

def _format_message(msg: OutboxMessage) -> Text | None:
    """Convert an OutboxMessage to a Rich Text renderable.

    NOTE: agent / status / error / trace / skill_done / intervention are all
    handled in render_message() with dedicated widgets and never reach here.
    This helper only formats unknown / fallback kinds.
    """
    meta_pfx = _meta_prefix(msg.meta)
    body = f"{meta_pfx}{msg.text}"

    if msg.kind in {"__end__", "__attach_request__", "intervention",
                    "agent", "system", "status", "error", "trace", "skill_done"}:
        return None
    # Unknown kind — show raw with subtle prefix
    t = Text()
    t.append(f"[{msg.kind}] ", style="dim")
    t.append(body)
    return t


def _format_intervention_line(msg: OutboxMessage) -> Text:
    """Fallback inline line for intervention when no widget callback set."""
    t = Text()
    t.append("  Aria asks  ", style="bold " + _CORAL)
    t.append(msg.text, style="#ffcc88")
    return t
