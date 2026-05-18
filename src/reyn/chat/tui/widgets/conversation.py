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
  called after a turn ends and writes a dim "⌁ Δ tokens │ $0.XXXX" line.
"""
from __future__ import annotations

import time

from rich.cells import cell_len
from rich.console import RenderableType
from rich.markdown import Markdown as RichMarkdown
from rich.padding import Padding
from rich.text import Text
from textual.app import ComposeResult
from textual.widget import Widget
from textual.widgets import RichLog, Static

from reyn.chat.outbox import OutboxMessage
from reyn.chat.tui._palette import _AMBER, _CORAL

from .error_box import ErrorBox
from .intervention import InterventionWidget
from .skill_activity import SkillActivityRow
from .sticky_status import StickyStatus
from .streaming_row import StreamingRow

_DASH_TOTAL = 38  # matches the banner separator width
_GROUP_WINDOW_S = 60.0  # consecutive turns within this window share a header
# Speaker-identity glyphs prepended to header labels — non-color cue so
# turn boundaries stay legible in greyscale / for color-blind users.
# The header name color (cyan / amber / grey) is still the primary
# signal; the glyph is a redundant shape signal so neither channel
# alone is load-bearing.
_GLYPH_USER = "▶"
_GLYPH_AGENT = "◆"
_GLYPH_SYSTEM = "·"
_FOLD_THRESHOLD_LINES = 30  # B3: agent replies above this fold inline
# /copy ring-buffer depth. Far enough back that the user can grab "the
# reply two turns ago" — the typical "wait, that one was useful" recovery
# pattern — without growing memory unboundedly across long sessions.
_RECENT_REPLIES_MAX = 10
# RichLog ring-buffer size. Bumped from the historical 5000 → 20000 to push
# the truncation boundary well past realistic session lengths (~400-800
# turns at typical reply sizes). Storage stays modest at average line width;
# turn anchors below are drop-aware so even pathological sessions that DO
# cross the boundary don't break Ctrl+P/N navigation.
_RICHLOG_MAX_LINES = 20_000
_NAME_COL_COLS = 4  # display-cell width reserved for the speaker label column
# Hanging indent for message bodies (= the lines under each header). The
# header is ``HH:MM  reyn ───`` — 5 (timestamp) + 2 (gap) = 7 cells before
# the name column starts. Indenting the body to column 7 visually nests
# replies under their author's name and, crucially, makes wrap continuations
# of a long body line visually distinct from the start of a new turn
# (which begins at column 0). Without this, a wrapped URL or code-line
# looks identical to a new ``HH:MM …`` header on a quick scan.
_BODY_INDENT_COLS = 7


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


def _indent_body(renderable: RenderableType) -> RenderableType:
    """Wrap ``renderable`` in a left-only Padding for the body indent column.

    Used at every body write site (agent markdown, user text, system text,
    fallback formatted lines). Header writes intentionally bypass this
    helper so the timestamp / name / dash rule stay anchored at column 0.
    """
    return Padding(renderable, (0, 0, 0, _BODY_INDENT_COLS))


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


def _is_lifecycle_marker(text: str) -> bool:
    """Heuristic: ``ChatLifecycleForwarder`` marker text starts with ``[↑``
    and ends with ``]``, and is single-line.

    Lifecycle markers (= compaction, future attach-budget signals) are
    state-change announcements, not speech — rendering them as a full
    speaker-tagged system block (= timestamp + ``· system`` header + dash
    rule + indented body) gives them more visual weight than they
    deserve. The conv pane routes them through ``_render_lifecycle_marker``
    so they appear as a dim inline divider, matching the date-separator
    style.
    """
    t = text.strip()
    return t.startswith("[↑") and t.endswith("]") and "\n" not in t


def _render_lifecycle_marker(text: str) -> Text:
    """Dim ``── ↑ <body> ────…`` inline divider for a lifecycle marker.

    Shape mirrors ``_date_separator`` — same total cell width (``_DASH_TOTAL``)
    and same ``dim #666666`` styling so the visual rhythm stays consistent
    with day-boundary markers and other inline dividers.
    """
    stripped = text.strip().lstrip("[").rstrip("]").strip()
    label = f" {stripped} "
    t = Text()
    lead_dashes = 2
    label_cells = cell_len(label)
    trail_dashes = max(1, _DASH_TOTAL - lead_dashes - label_cells)
    t.append("─" * lead_dashes, style="dim #666666")
    t.append(label, style="dim #666666")
    t.append("─" * trail_dashes, style="dim #666666")
    return t


def _date_separator(date_str: str) -> Text:
    """Dim ``── YYYY-MM-DD ───…`` line for day boundaries between turns.

    Same total cell width as a regular turn header (_DASH_TOTAL) so the
    layout stays consistent. Rendered in the same dim grey as the
    timestamp column so the visual weight is below speaker headers but
    above body text.
    """
    t = Text()
    label = f" {date_str} "
    # Two leading dashes to balance the line visually; remaining dashes
    # extend to _DASH_TOTAL.
    lead_dashes = 2
    label_cells = cell_len(label)
    trail_dashes = max(1, _DASH_TOTAL - lead_dashes - label_cells)
    t.append("─" * lead_dashes, style="dim #666666")
    t.append(label, style="dim #666666")
    t.append("─" * trail_dashes, style="dim #666666")
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
        # Last date (YYYY-MM-DD) we wrote a header for. When a new header
        # crosses to a different calendar day, emit a dim date-separator
        # line first so users can tell which day's "21:55" they're looking
        # at in a multi-day session.
        self._last_header_date: str = ""
        # Last turn-flash position written by ``_flash_turn_position`` —
        # (n, total) tuple. Used to suppress duplicate "↑ turn N / M"
        # log lines when the user mashes Ctrl+P/N within the same anchor.
        self._last_turn_flash: tuple[int, int] | None = None
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
        # Recent agent replies (newest last), capped at ``_RECENT_REPLIES_MAX``.
        # Consumed by the /copy slash command — users can grab the latest reply
        # (``/copy``) or any of the last N (``/copy 2``, ``/copy 3``, …) without
        # fighting the TUI's mouse-capture to drag-select text out of the log.
        # Single-slot storage silently lost every prior reply on each new turn;
        # a bounded ring keeps the immediate history reachable without growing
        # memory unboundedly across long sessions.
        self._recent_replies: list[str] = []
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

    def on_mount(self) -> None:
        """Wire a scroll watcher so user scroll-up suppresses auto-scroll.

        Previously the ``_user_scrolled`` flag existed but nothing ever
        set it: every ``log.write(...)`` snapped the view back to the
        bottom even mid-read. Watching ``scroll_y`` from a single reactive
        callback distinguishes "user is reading old content" (= scroll_y
        below max) from "stream just appended" (= scroll_y already at
        max because Textual's auto_scroll moved it there). The watcher
        only flips ``auto_scroll`` on the boundary crossing, so writes
        during user-read keep their place and writes after user-return
        immediately auto-scroll again.
        """
        log = self._log()
        try:
            self.watch(log, "scroll_y", self._on_log_scroll_y)
        except Exception:
            # If Textual's cross-widget watch API changes, fail open
            # (= keep historic auto-scroll behaviour) rather than crash mount.
            pass

    def _snap_to_bottom(self) -> None:
        """Force the log to the bottom and re-arm auto-scroll.

        Called from "I'm re-engaging" entry points (``render_user_message``,
        ``clear``) so the user's previous scroll-up state doesn't pin them
        to old content after they've explicitly taken an action.
        """
        try:
            log = self._log()
        except Exception:
            return
        try:
            log.auto_scroll = True
            log.scroll_end(animate=False)
        except Exception:
            pass
        self._user_scrolled = False

    def _on_log_scroll_y(self, old: float, new: float) -> None:
        """Flip ``auto_scroll`` and ``_user_scrolled`` based on at-bottom check.

        The ``-1`` threshold absorbs float-coord noise from Textual's
        scroll math; treating "within 1 cell of the bottom" as "at the
        bottom" matches how the user perceives the boundary.
        """
        try:
            log = self._log()
        except Exception:
            return
        at_bottom = new >= log.max_scroll_y - 1
        # Only re-assign when the value actually changes so we don't churn
        # Textual's reactive system every scroll tick.
        if at_bottom:
            if not log.auto_scroll:
                log.auto_scroll = True
            if self._user_scrolled:
                self._user_scrolled = False
        else:
            if log.auto_scroll:
                log.auto_scroll = False
            if not self._user_scrolled:
                self._user_scrolled = True

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
            # Day boundary marker: when this header lands on a different
            # calendar day than the previous one (or the session's first
            # header), emit a dim "── YYYY-MM-DD ─────" line so users in a
            # multi-day session can tell which "21:55" they're looking at.
            today = time.strftime("%Y-%m-%d")
            if today != self._last_header_date:
                log.write(_date_separator(today))
                self._last_header_date = today
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
            self._write_body(text)

    def render_user_message(self, text: str) -> None:
        """Render a freshly submitted user message with grouped header.

        Submitting a message is an "I'm re-engaging" signal — even if the
        user had previously scrolled up to read history, they now want to
        see the conversation continue. Snap back to the bottom and re-arm
        auto-scroll so subsequent agent reply chunks track the tail.
        """
        self._snap_to_bottom()
        self._consume_empty_hint()
        self._maybe_write_header(
            "you", f"{_GLYPH_USER} you", "bold #4abbb5", "#666666",
        )
        self._write_body(Text(text))
        self._write_log(Text(""))

    def _render_system_message(self, msg: OutboxMessage) -> None:
        """Render a slash-command (or other OS-generated) message persistently.

        Distinct from ``agent`` so the log doesn't claim the LLM produced
        these lines, and distinct from ``status`` so prior outputs survive
        when running multiple commands in a row.

        Rendered as plain text (newlines preserved, no Markdown) under a
        neutral ``system`` header in dim grey. **Exception**: lifecycle
        markers (= ``[↑ ... ]`` shape from ``ChatLifecycleForwarder``)
        skip the speaker header and render as a dim inline divider —
        they're state-change announcements, not speech, and don't
        deserve the same visual weight as a slash-command output.
        """
        self._consume_empty_hint()
        self.hide_status()
        text = msg.text or ""
        if _is_lifecycle_marker(text):
            self._write_log(_render_lifecycle_marker(text))
            return
        self._maybe_write_header(
            "system", f"{_GLYPH_SYSTEM} system", "bold #888888", "#666666",
        )
        for line in text.splitlines() or [""]:
            self._write_body(Text(line))
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
        # _AMBER for agent identity (header label) — distinct from _CORAL
        # which is reserved for interactive affordances (/expand, picker
        # selection caret, panel cursor ▶) and "you are here" indicators.
        self._maybe_write_header(
            "reyn", f"{_GLYPH_AGENT} {label}", "bold " + _AMBER, "#666666",
        )
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

        Side effect: appends ``text`` to ``self._recent_replies`` (capped
        at ``_RECENT_REPLIES_MAX``) so the /copy slash command can hand
        any of the last N replies to the system clipboard.
        """
        # Always remember the full text — independent of fold thresholds.
        self._recent_replies.append(text)
        if len(self._recent_replies) > _RECENT_REPLIES_MAX:
            self._recent_replies = self._recent_replies[-_RECENT_REPLIES_MAX:]
        log = self._log()
        # Detect that a prior fold is about to be invalidated. The single-slot
        # ``_last_long_reply`` is replaced (or cleared) by every new agent
        # reply, so any old fold hint up-screen still reads "type /expand to
        # show" while /expand itself silently no-ops. Flag it inline so the
        # user can tell the previous fold is no longer reachable.
        had_prev_fold = self._last_long_reply is not None
        lines = text.split("\n")
        if len(lines) <= _FOLD_THRESHOLD_LINES:
            self._write_body(RichMarkdown(text))
            if had_prev_fold:
                self._write_fold_expired_marker()
            self._last_long_reply = None
            return
        preview = "\n".join(lines[:_FOLD_THRESHOLD_LINES])
        remaining = len(lines) - _FOLD_THRESHOLD_LINES
        self._write_body(RichMarkdown(preview))
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
        self._write_body(RichMarkdown(self._last_long_reply))
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
        return self._recent_replies[-1] if self._recent_replies else None

    def reply_at(self, n: int) -> str | None:
        """Return the n-th most recent agent reply (1-indexed; n=1 is latest).

        Returns None when ``n`` is out of range (≤ 0 or beyond the buffered
        history). The /copy slash uses this to surface older replies that the
        single-slot predecessor silently lost on every new turn.
        """
        if n <= 0 or n > len(self._recent_replies):
            return None
        return self._recent_replies[-n]

    def recent_reply_count(self) -> int:
        """Number of agent replies currently held in the /copy ring buffer."""
        return len(self._recent_replies)

    def _write_log(self, text: Text) -> None:
        log = self._log()
        log.write(text)

    def _write_body(self, renderable: RenderableType) -> None:
        """Append a body renderable at the hanging-indent column.

        Wraps ``renderable`` in left-only ``Padding`` so wrap continuations
        line up under the speaker name and stay visually distinct from the
        column-0 turn header. Used for agent markdown, user / system text,
        and any other content that belongs "under" the most recent header.
        """
        self._log().write(_indent_body(renderable))

    # ── streaming support ─────────────────────────────────────────────────────

    def begin_stream(self, msg_id: str, agent_name: str = "") -> StreamingRow:
        """Start a streaming agent message row. Returns the row widget."""
        self._consume_empty_hint()
        label = agent_name if agent_name else "reyn"
        # Same agent-identity styling as _render_agent_markdown (_AMBER).
        self._maybe_write_header(
            "reyn", f"{_GLYPH_AGENT} {label}", "bold " + _AMBER, "#666666",
        )
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

    def update_skill_detail(self, run_id: str, detail: str) -> None:
        """Update the row's in-phase detail (=``⤷ <detail>`` segment).

        No-op if the row isn't mounted yet (a detail trace can arrive
        before the first ``phase_started``; the row will be lazy-mounted
        on the next phase event and will pick up subsequent details).
        """
        row = self._skill_rows.get(run_id)
        if row is not None:
            row.set_detail(detail)

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
        # Same scroll-respect rule as ``mount_intervention``: when the
        # user has scrolled up to read prior context, an async error
        # arriving must not yank the view to the bottom; the error box
        # carries its own non-color cue (left-bar) and the user can
        # discover it on their next scroll-down without being interrupted.
        if not self._user_scrolled:
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
        # Only yank the user down to the new widget when they were
        # already at the tail. If they've scrolled up to read history,
        # an async intervention arriving must not jerk the view — they
        # can see the prompt waiting at the bottom via the scrollbar /
        # auto_scroll once they return on their own, and ``hide_status``
        # above already replaced the live "thinking…" so they have a
        # clear signal that the run is waiting for them.
        if not self._user_scrolled:
            try:
                widget.scroll_visible()
            except Exception:
                pass
        return widget

    # ── cost suffix (A4) ──────────────────────────────────────────────────────

    def render_cost_suffix(self, tokens: int, cost_usd: float, elapsed_s: float) -> None:
        """Append a dim per-turn cost suffix, right-aligned. Caller decides when (opt-in).

        Both pieces are load-bearing: ``Text(..., justify="right")`` only
        right-aligns when the renderer is told a width to fill, and
        ``RichLog.write`` defaults to ``expand=False`` — without
        ``expand=True`` the suffix silently renders at column 0.

        Separator is ``│`` (U+2502, narrow, unambiguous width) rather than
        ``·`` (U+00B7, East Asian Width "Ambiguous") so Rich's cell-width
        accounting matches what the terminal actually paints — the previous
        ``·`` rendered as 2 cells on some terminals, pushing the elapsed
        tail past the right edge and clipping ``14.3s`` to ``·`` alone.
        Matches the header's existing ``│`` separator for visual consistency.
        """
        t = Text(
            f"⌁ {tokens}t │ ${cost_usd:.4f} │ {elapsed_s:.1f}s",
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
        # Show "turn N / M" feedback so users in long sessions can tell
        # where they are. Without this Ctrl+P/N scrolls silently and a
        # 90-turn history is just a parade of "21:55" headers with no
        # cursor signal. (StickyStatus with kind="general" was the
        # intuitive surface but doesn't render reliably from this code
        # path — open question, see workload notes; the log marker is
        # the working compromise.)
        try:
            idx = anchors.index(target)
        except ValueError:
            return
        self._flash_turn_position(idx + 1, len(anchors))

    def _flash_turn_position(self, n: int, total: int) -> None:
        """Write a dim ``↑ turn N / M`` line to the conv log.

        Deduped by ``_last_turn_flash`` so rapid Ctrl+P/N presses that
        land on the same anchor don't spam the log with identical lines.
        Cleared on Ctrl+L so post-clear navigation flashes fresh.
        """
        if self._last_turn_flash == (n, total):
            return
        self._last_turn_flash = (n, total)
        self._write_log(Text(f"  ↑ turn {n} / {total}", style="dim italic #666666"))

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
        self._last_header_date = ""
        self._last_turn_flash = None
        self._turn_anchors.clear()
        self._trim_warned = False
        self._last_long_reply = None
        # Re-arm auto-scroll: clear() puts the user back at a fresh blank
        # log, and any prior scroll-up state is meaningless once the
        # content it was reading is gone.
        self._user_scrolled = False
        try:
            self._log().auto_scroll = True
        except Exception:
            pass
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
    # Intervention prefix is agent-identity (= the agent asking a question),
    # so it matches the agent header colour (_AMBER), not the action colour.
    t.append("  Aria asks  ", style="bold " + _AMBER)
    t.append(msg.text, style="#ffcc88")
    return t
