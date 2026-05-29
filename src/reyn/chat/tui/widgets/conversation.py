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

import logging
import time
from typing import Literal

from rich.cells import cell_len
from rich.console import RenderableType
from rich.markdown import Markdown as RichMarkdown
from rich.padding import Padding
from rich.text import Text
from textual.app import ComposeResult
from textual.widget import Widget
from textual.widgets import RichLog, Static

# Wave-10 follow-up G-F15: module logger for the one-shot
# Textual-API-change warning in ``_richlog_start_line``.
logger = logging.getLogger(__name__)

from reyn.chat.outbox import OutboxMessage
from reyn.chat.tui._palette import _AMBER, _CORAL

from .async_stack_panel import AsyncStackPanel
from .error_box import ErrorBox
from .foldable_markdown import FoldableMarkdown
from .inline_thinking_row import InlineThinkingRow
from .intervention import InterventionWidget
from .skill_activity import SkillActivityRow
from .sticky_status import StickyStatus
from .streaming_row import StreamingRow
from .tool_call_row import ToolCallRow

_DASH_TOTAL = 38  # matches the banner separator width
_GROUP_WINDOW_S = 60.0  # consecutive turns within this window share a header
# Speaker-identity symbols — 1-char shape cues for turn boundaries.
# These replace the old "▶ you" / "◆ reyn" labels; the symbol alone
# differentiates speaker without the redundant label text.
_GLYPH_USER = ">"
_GLYPH_AGENT = "⏺"
_GLYPH_SYSTEM = "·"
_FOLD_THRESHOLD_LINES = 30  # B3: rendered-screen-line estimate above this folds inline
# Hanging indent constants — two modes gated by ``_show_timestamps``.
# ON  (ts shown):  ``HH:MM <sym>`` = 5 (ts) + 1 (space) + 1 (sym) + 1 (space)
#                  → body starts col 8.
# OFF (ts hidden): ``<sym>`` = 1 (sym) + 1 (space) → body starts col 2.
_BODY_INDENT_WITH_TS = 8
_BODY_INDENT_NO_TS = 2
# Legacy alias kept so external code that imports _BODY_INDENT_COLS
# directly (e.g. streaming_row.py, foldable_markdown.py, tests) still
# compiles. Points at the ts-on value (= the default).
_BODY_INDENT_COLS = _BODY_INDENT_WITH_TS
# Public alias for cross-module invariant tests that pin
# ``streaming_row.BODY_INDENT_COLS == conversation.BODY_INDENT_COLS``.
BODY_INDENT_COLS = _BODY_INDENT_COLS
_FOLD_WIDTH_FALLBACK = 73   # estimated body width when size.width is 0 (= 80 - _BODY_INDENT_WITH_TS)
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
# F-H: minimum visible duration for an inline ToolCallRow before the conv
# pane flushes it into the RichLog scroll history + unmounts the live
# widget. Cache hits / instant returns would otherwise mount + flush
# within a single event-loop tick, leaving no perceptual cue. 0.3s
# matches typical perceptual threshold (= "I saw something happen").
_TOOL_CALL_MIN_DISPLAY_S = 0.3
# Cap on simultaneously-mounted ErrorBox widgets. Past this, the oldest
# rolls into a dim ``_write_log`` breadcrumb (= same shape as the F2
# Esc-dismissed breadcrumb) so the footer area can't pile up under a
# burst of failures (e.g. proxy down + multiple retries).
_MAX_VISIBLE_ERROR_BOXES = 3


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


def _indent_body(renderable: RenderableType, indent: int = _BODY_INDENT_WITH_TS) -> RenderableType:
    """Wrap ``renderable`` in a left-only Padding for the body indent column.

    Used at every body write site (agent markdown, user text, system text,
    fallback formatted lines). Header writes intentionally bypass this
    helper so the timestamp / symbol stay anchored at column 0.

    ``indent`` defaults to the ts-on value (= 8). Callers that track
    ``_show_timestamps`` pass the dynamic value via
    ``ConversationView._current_body_indent()``.
    """
    return Padding(renderable, (0, 0, 0, indent))


def _msg_header(symbol: str, name_style: str, show_ts: bool = True) -> Text:
    """Symbol-only header for a new message turn.

    Layout when ``show_ts=True``:
        ``HH:MM <symbol>``  (5 ts + 1 space + 1 symbol)
        body starts at col 8 (= _BODY_INDENT_WITH_TS)

    Layout when ``show_ts=False``:
        ``<symbol>``
        body starts at col 2 (= _BODY_INDENT_NO_TS)

    The dash rule and label text (``▶ you`` / ``◆ reyn``) are intentionally
    dropped — the 1-char symbol already differentiates speaker, and blank
    lines between turns provide turn separation without horizontal noise.
    """
    t = Text()
    if show_ts:
        t.append(time.strftime("%H:%M"), style="dim #666666")
        t.append(" ")
    t.append(symbol, style=name_style)
    return t


def _build_header_prefix(symbol: str, name_style: str, show_ts: bool = True) -> Text:
    """Build the ``HH:MM <symbol> `` (trailing space) inline prefix Text.

    Same glyph/timestamp logic as ``_msg_header`` but appends a trailing
    space so the body can be concatenated directly to produce the Claude
    Code-style ``HH:MM > body text`` inline layout.

    Used by ``_write_inline_header_body`` to combine the header and first
    body line into a single RichLog write, which puts the speaker symbol
    and message content on the same visual line with col-8 hanging indent
    for wrap continuations.
    """
    t = Text()
    if show_ts:
        t.append(time.strftime("%H:%M"), style="dim #666666")
        t.append(" ")
    t.append(symbol, style=name_style)
    t.append(" ")  # trailing space so body starts immediately after symbol
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


# W13 A#3 — soft severity classifier for the TUI seam (no engine changes).
# HIGH: terminal failures the operator must act on immediately.
# MED:  recoverable / transient — default for unclassified errors.
# LOW:  user-input mistakes that self-resolve on the next valid command.
_HIGH_TEXT_MARKERS: tuple[str, ...] = (
    "[budget exceeded]",
    "[auth error]",
    "[permission denied]",
)
_HIGH_META_SOURCE_SUFFIXES: tuple[str, ...] = ("_failed", "_aborted")
_LOW_TEXT_PREFIXES: tuple[str, ...] = ("usage:", "unknown command")


def _classify_error_severity(
    message: str,
    meta: dict,
) -> Literal["high", "med", "low"]:
    """Classify an error message into a 3-tier severity.

    HIGH — terminal failure (budget / auth / permission) or a meta source
    that ends with ``_failed`` / ``_aborted``.

    LOW — user input mistake: message starts with ``usage:`` or
    ``unknown command`` (case-insensitive).

    MED — everything else (recoverable / transient / unclassified default).

    TUI-internal helper only — no engine imports, no OS changes (P7).
    """
    lower = message.lower().lstrip()
    # Meta source suffix check (HIGH).
    source = str((meta or {}).get("source") or "")
    if source and any(source.endswith(s) for s in _HIGH_META_SOURCE_SUFFIXES):
        return "high"
    # Text-marker check (HIGH).
    msg_lower = message.lower()
    if any(marker in msg_lower for marker in _HIGH_TEXT_MARKERS):
        return "high"
    # User-input mistake (LOW).
    if any(lower.startswith(p) for p in _LOW_TEXT_PREFIXES):
        return "low"
    return "med"


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

    def __init__(self, *, id: str | None = None) -> None:
        super().__init__(id=id)
        self._stream_rows: dict[str, StreamingRow] = {}
        self._skill_rows: dict[str, SkillActivityRow] = {}
        # Issue #427 step 4: per-tool_call inline rows keyed by op_id
        # (= dispatch_tool's args_hash, propagated via forwarder OutboxMessage
        # meta). Mounted on tool_call_started, finalised on
        # tool_call_completed / tool_call_failed.
        self._tool_call_rows: dict[str, ToolCallRow] = {}
        # W13 T2-1: reference to the most-recent ToolCallRow that was
        # transitioned to failure state, kept so the F7 keyboard drill-down
        # action can toggle its expand state. The reference is cleared on
        # Ctrl+L (clear()) and updated on every fail_tool_call_row() call.
        # The row may still be mounted (= live widget, F7 can toggle expand)
        # or already flushed into the RichLog (= widget removed; F7 surfaces
        # a "Ctrl+B → events" hint instead). A None value means no failure
        # has occurred yet in this session.
        self._last_failed_tool_row: ToolCallRow | None = None
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
        # Wave-3 FS2: separate dedup state for the boundary hint
        # (``↑ beginning of history`` / ``↓ end of history``) so rapid
        # Ctrl+P/N at the edge doesn't spam the log. Reset on clear().
        self._last_boundary_flash: str | None = None
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
        # Wave-10 follow-up G-F15: one-shot flag for the "RichLog
        # private API broke" warning in ``_richlog_start_line``. Prevents
        # log spam when a Textual upgrade removes the ``_start_line``
        # attribute we depend on for turn-navigation anchors.
        self._start_line_warned = False
        # B3 — full text of the last truncated agent reply (or None when the
        # most recent reply fit within _FOLD_THRESHOLD_LINES).
        # DEPRECATED: retained for has_pending_expand property only.
        # Long replies are now mounted as FoldableMarkdown widgets tracked
        # in ``_foldables`` below. ``expand_last_reply`` is kept for
        # backwards-compat but delegates to toggle_last_foldable().
        self._last_long_reply: str | None = None
        # B3 (toggle): ordered list of mounted FoldableMarkdown widgets,
        # newest last. toggle_last_foldable() operates on the tail element.
        # Cleared on clear(). Does NOT grow unboundedly — FoldableMarkdown
        # widgets are height:auto children; the list is a reference list only.
        self._foldables: list[FoldableMarkdown] = []
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
        # Cursor into ``_error_boxes`` for the F5 / F6 jump-to-error
        # navigation. ``-1`` means "no cursor yet" (= first jump
        # targets the newest error). Invalidated whenever a dismiss
        # or auto-eviction mutates ``_error_boxes`` so the next jump
        # re-seeds from the (now-different) newest error.
        self._error_jump_cursor: int = -1
        # F9 timestamp toggle — default on. Loaded from tui_prefs.json in
        # on_mount; callers use toggle_timestamps() / show_timestamps property.
        # New messages rendered after a toggle use the new indent; past
        # messages stay at whatever indent they had when rendered (= no
        # full re-render of scroll history).
        self._show_timestamps: bool = True

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
            # Three-line layout (each ~40-50 cells) so the hint stays
            # legibly stacked at the narrowest reachable conv-pane
            # width. The previous single line 2 carried the slash
            # mechanic + tab list + keybind set on one line at ~95
            # cells, which wrapped to 4–5 ragged lines when the right
            # panel was expanded toward its max width and the conv pane
            # shrank to ~50 cols. Splitting into purpose / contents /
            # keybind preserves the side-panel discoverability the test
            # at ``test_empty_hint_discoverability.py`` defends, while
            # making each line short enough that wrap is at worst 2
            # clean lines per stanza on any terminal we expect.
            "  Type [bold]/[/] for commands  ·  [bold]/help[/] for a guide\n"
            "  [bold]Ctrl+B[/] side panel ·  [bold]Ctrl+L[/] clear  ·  [bold]Ctrl+P/N[/] turn\n"
            "  [dim]panel tabs: keys · events · agents · memory · cost · docs · pending"
            "  ([bold]Tab[/]/[bold]Ctrl+W[/] cycle)[/]",
            id="empty-hint",
            markup=True,
        )
        # A3: sticky status pinned to bottom of conv pane (= 1-line
        # ``⟳ thinking…`` / ``⚑ awaiting answer`` live indicator).
        # AsyncStackPanel docks ABOVE the sticky (= the 0-5-line
        # "currently running tasks" overview). Per Textual's
        # ``dock: bottom`` stacking rule (= multiple bottom-docked
        # siblings stack in declaration order, with each new sibling
        # placed further FROM the bottom edge), yielding StickyStatus
        # first pins it to the very bottom; the subsequent
        # AsyncStackPanel sits above it. Result from bottom-up:
        # ``input bar → sticky → async stack → conv log``.
        # AsyncStackPanel collapses to zero height when no tasks are
        # active so the input bar layout is unchanged in the
        # cold-default case.
        yield StickyStatus(id="sticky-status")
        yield AsyncStackPanel(id="async-stack")

    def _log(self) -> RichLog:
        return self.query_one("#log", RichLog)

    def _sticky(self) -> StickyStatus | None:
        try:
            return self.query_one("#sticky-status", StickyStatus)
        except Exception:
            return None

    def _async_stack(self) -> AsyncStackPanel | None:
        """Return the bottom-docked AsyncStackPanel, if mounted.

        Mirrors ``_sticky()`` — the panel may not exist during
        composition in test harnesses that mount ConversationView
        manually without going through the full compose path.
        """
        try:
            return self.query_one("#async-stack", AsyncStackPanel)
        except Exception:
            return None

    def add_async_task(self, task_id: str, summary: str) -> None:
        """Add or update an attached-agent task in the bottom stack panel.

        Wiring helper for the production hook (= app.py's
        ``_handle_trace_for_skill_row`` calls this on the
        ``"phase started:"`` first-trace branch and on plan
        spawn events). ``task_id`` is the canonical task identity
        — ``run_id`` for skill spawns or ``plan_id`` for plan
        spawns; both flow through this single entry point.

        Silent no-op when the panel isn't mounted (= test
        harness path), matching the ``show_status`` / ``hide_status``
        defensive style.
        """
        panel = self._async_stack()
        if panel is None or not task_id:
            return
        panel.add(task_id, summary)

    def remove_async_task(
        self,
        task_id: str,
        *,
        terminal: str = "ok",
    ) -> None:
        """Drop ``task_id``'s entry from the bottom stack panel.

        Called on ``"skill done:"`` from the trace flow + the
        corresponding plan-completion path. Idempotent (= silent
        no-op if the panel isn't mounted or the task is unknown).

        Wave-13 T2-2: ``terminal`` is threaded through to
        ``AsyncStackPanel.remove`` so aborted / interrupted lifecycle
        events can trigger the red flash-before-unmount behaviour.
        Valid values: ``"ok"`` (default, immediate unmount),
        ``"aborted"``, ``"interrupted"``.
        """
        panel = self._async_stack()
        if panel is None or not task_id:
            return
        # Coerce to the Literal values AsyncStackPanel.remove accepts.
        # Anything unrecognised falls back to "ok" (= existing safe default).
        if terminal in ("aborted", "interrupted"):
            panel.remove(task_id, terminal=terminal)  # type: ignore[arg-type]
        else:
            panel.remove(task_id)

    def clear_async_tasks(self) -> None:
        """Reset the bottom stack panel to empty.

        Called from ``clear()`` so a Ctrl+L wipes the running-task
        overview alongside the conv log. The visible stack would
        otherwise survive the clear and look like "ghost rows"
        attached to a fresh-looking pane.
        """
        panel = self._async_stack()
        if panel is None:
            return
        panel.clear()

    # ── timestamp toggle (F9) ─────────────────────────────────────────────────

    @property
    def show_timestamps(self) -> bool:
        """True when the ``HH:MM`` prefix is prepended to speaker headers."""
        return self._show_timestamps

    def _current_body_indent(self) -> int:
        """Return the dynamic body-indent column based on ``_show_timestamps``.

        ON  → ``_BODY_INDENT_WITH_TS`` (8) — body under the symbol when ts shown.
        OFF → ``_BODY_INDENT_NO_TS``  (2) — symbol at col 0, body at col 2.
        """
        return _BODY_INDENT_WITH_TS if self._show_timestamps else _BODY_INDENT_NO_TS

    def toggle_timestamps(self) -> bool:
        """Flip the timestamp-visibility state and persist to ``tui_prefs.json``.

        Returns the NEW state (True = ts now visible, False = now hidden).
        The toggle applies to NEW messages only (= no re-render of past
        history). Past messages stay at whatever indent they were rendered
        with — a full re-render would be expensive and confusing mid-session.
        """
        self._show_timestamps = not self._show_timestamps
        try:
            from reyn.chat.tui.prefs import load_tui_prefs, save_tui_prefs
            root = None
            try:
                root = self.app._project_root_path()  # type: ignore[attr-defined]
            except Exception:
                pass
            prefs = load_tui_prefs(root)
            prefs["show_timestamps"] = self._show_timestamps
            save_tui_prefs(root, prefs)
        except Exception:
            pass
        return self._show_timestamps

    # ── Public state accessors (Tier C Path C — replaces private-attr asserts) ─

    @property
    def user_scrolled(self) -> bool:
        """True when the user has manually scrolled away from the tail.

        Read-only accessor for the ``_user_scrolled`` latch. Tests and
        external observers should read this property rather than accessing
        ``_user_scrolled`` directly.
        """
        return self._user_scrolled

    @property
    def trim_warned(self) -> bool:
        """True when the one-shot ring-buffer-trim warning has been emitted.

        The latch fires at most once per session (or per clear()). Tests
        that need to verify whether the trim warning fired should use this
        property rather than accessing ``_trim_warned`` directly.
        """
        return self._trim_warned

    @property
    def last_long_reply(self) -> str | None:
        """Tail text of the most recent long agent reply (= fold tail), or None.

        Mirrors the ``_last_long_reply`` slot: set when the most recent
        reply triggered the B3 fold (= too long for inline), cleared when
        a subsequent short reply fits inline. Use ``has_pending_expand``
        for the boolean form; this property exposes the raw value for
        tests that need to distinguish *which* tail was stashed.
        """
        return self._last_long_reply

    @property
    def last_speaker_at(self) -> float:
        """Wall-clock timestamp (``time.time()`` epoch seconds) of the last header write.

        Used by header-grouping tests to verify the clock source is wall
        time and not monotonic. Read-only — tests that need to simulate
        time passage write ``conv._last_speaker_at`` directly for setup.
        """
        return self._last_speaker_at

    @property
    def last_header_date(self) -> str:
        """``YYYY-MM-DD`` string of the last date-separator written, or ``""`` before any message.

        ``clear()`` updates this to today so same-day Ctrl+L doesn't
        re-emit the date separator. Tests verify this via the property
        rather than accessing ``_last_header_date`` directly.
        """
        return self._last_header_date

    def turn_anchors_snapshot(self) -> tuple[int, ...]:
        """Return a snapshot of the current turn-anchor list as an immutable tuple.

        Each value is an absolute line position (drop-aware, monotonically
        growing). Tests should use this rather than accessing
        ``_turn_anchors`` directly.
        """
        return tuple(self._turn_anchors)

    @property
    def tool_call_row_ids(self) -> frozenset:
        """Frozenset of op_id strings for currently-tracked in-flight tool-call rows.

        Supports membership tests (``op_id in conv.tool_call_row_ids``),
        emptiness checks (``not conv.tool_call_row_ids``), and equality
        (``conv.tool_call_row_ids == frozenset()``). Tests should use this
        rather than accessing ``_tool_call_rows`` directly.
        """
        return frozenset(self._tool_call_rows)

    def error_box_count(self) -> int:
        """Number of currently-mounted (undismissed) ErrorBox widgets.

        Tests should use this rather than accessing ``_error_boxes`` directly.
        Zero means no live error boxes; ``> 0`` means at least one is
        mounted and visible. See also ``has_error_boxes()`` for the boolean
        form.
        """
        return len(self._error_boxes)

    def richlog_start_line(self, log: "RichLog") -> int:
        """Public wrapper around ``_richlog_start_line`` for test access.

        Returns the RichLog's cumulative dropped-lines counter (=
        ``log._start_line``) with a one-shot warning when the attribute is
        missing. Tests that verify the ring-buffer trim / anchor-projection
        logic should call this rather than ``_richlog_start_line`` directly.
        """
        return self._richlog_start_line(log)

    def absolute_line_position(self, log: "RichLog") -> int:
        """Public wrapper around ``_absolute_line_position`` for test access.

        Returns ``richlog_start_line(log) + len(log.lines)`` — the
        monotonically-growing absolute write position. Tests that verify
        anchor storage should call this rather than ``_absolute_line_position``
        directly.
        """
        return self._absolute_line_position(log)

    def async_stack_snapshot(self) -> list:
        """Return the AsyncStackPanel's current snapshot list, or ``[]`` if absent.

        Mirrors the ``AsyncStackPanel.snapshot()`` return shape: a list of
        dicts with at minimum ``agent_id``, ``summary``, and ``is_overflow``
        keys. Returns an empty list when the panel is not mounted (= test
        harness path or pre-compose). Tests should call this rather than
        accessing ``_async_stack().snapshot()`` directly.
        """
        panel = self._async_stack()
        if panel is None:
            return []
        return panel.snapshot()

    @property
    def stream_rows(self) -> "dict[str, StreamingRow]":
        """Shallow copy of the in-flight stream-row registry (msg_id → row).

        Tests that need to assert on stream routing (= which msg_ids are
        live, which have been sealed and removed) call this rather than
        accessing ``_stream_rows`` directly — per CLAUDE.md testing
        policy.  Returns a snapshot; do not mutate the returned dict.
        """
        return dict(self._stream_rows)

    @property
    def foldables(self) -> "list[FoldableMarkdown]":
        """A copy of the list of live ``FoldableMarkdown`` widgets.

        Populated by ``_write_agent_markdown_with_fold`` when a reply is
        long enough to fold. Tests use this to locate the widgets without
        reaching into ``_foldables`` directly — per CLAUDE.md testing
        policy. Returns a shallow list copy; do not mutate.
        """
        return list(self._foldables)

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

        Also loads ``show_timestamps`` from ``tui_prefs.json`` so the
        F9 toggle state survives a restart.
        """
        log = self._log()
        try:
            self.watch(log, "scroll_y", self._on_log_scroll_y)
        except Exception:
            # If Textual's cross-widget watch API changes, fail open
            # (= keep historic auto-scroll behaviour) rather than crash mount.
            pass
        # Load persisted timestamp-toggle state. The app instance holds
        # the project root; ConversationView reaches it defensively via
        # app.app (= the Textual ``app`` property on every widget).
        try:
            from reyn.chat.tui.prefs import load_tui_prefs
            root = None
            try:
                root = self.app._project_root_path()  # type: ignore[attr-defined]
            except Exception:
                pass
            prefs = load_tui_prefs(root)
            self._show_timestamps = bool(prefs.get("show_timestamps", True))
        except Exception:
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

    def _check_new_turn(self, speaker: str, log: "RichLog") -> bool:
        """Return True if this is a new turn (different speaker or window expired).

        Side-effects when a new turn starts:
          - Emits a date-separator when the calendar day has changed.
          - Records the current absolute line position as a turn anchor.
        Does NOT write any header line — the caller decides what to write.

        ``_last_speaker`` / ``_last_speaker_at`` are updated by the caller
        AFTER the header/body write (so anchors are recorded before the write
        that advances the line counter).

        Wave-10 follow-up G-F7: ``time.time()`` (wall clock) matches the
        visible ``HH:MM`` timestamp and grouping decision — see
        ``_maybe_write_header`` docstring for full rationale.
        """
        now = time.time()
        same_speaker = (speaker == self._last_speaker)
        within_window = (now - self._last_speaker_at) < _GROUP_WINDOW_S
        if same_speaker and within_window:
            return False
        # Day boundary marker.
        today = time.strftime("%Y-%m-%d")
        if today != self._last_header_date:
            log.write(_date_separator(today))
            self._last_header_date = today
        # Record a turn anchor.
        self._turn_anchors.append(self._absolute_line_position(log))
        return True

    def _maybe_write_header(self, speaker: str, symbol: str,
                             name_style: str) -> None:
        """Write a symbol-only header when the speaker changes or the gap exceeds _GROUP_WINDOW_S.

        The header is ``HH:MM <symbol>`` (ts on) or ``<symbol>`` (ts off).
        A blank line separates turns; the dash rule from the old layout is
        intentionally absent.

        Wave-10 follow-up G-F7: ``time.time()`` (wall clock) rather
        than ``time.monotonic()``. The header's visible HH:MM
        timestamp uses ``time.strftime`` (= wall clock), so grouping
        the *same* timeline keeps the displayed timestamp and the
        grouping decision in lockstep. ``monotonic`` doesn't advance
        during system sleep on every platform (= CLOCK_MONOTONIC vs
        CLOCK_BOOTTIME differ across Linux / macOS) — after a
        sleep/wake cycle, two messages can show wall-clock timestamps
        an hour apart yet share a grouping bucket simply because
        monotonic registered no progress. Wall clock matches the
        user-visible timeline exactly.
        """
        now = time.time()
        log = self._log()
        if self._check_new_turn(speaker, log):
            # No cap. The previous 200-entry cap silently dropped the
            # oldest anchors in long sessions, so Ctrl+P/N's "N / M"
            # readout showed an M smaller than the real turn count and
            # the user thought they had walked the entire history when
            # they had not. ``_resolve_anchors_to_current_view`` already
            # filters anchors whose line position fell below
            # ``log._start_line`` (= dropped by the RichLog ring
            # buffer), so the effective navigation list stays bounded
            # by ``_RICHLOG_MAX_LINES`` / typical-lines-per-turn even
            # though the raw ``_turn_anchors`` list grows unbounded.
            # The raw list cost is ~8 bytes per turn — a 24h session
            # generating one turn per second is ~700 KB, negligible
            # next to the RichLog itself.
            log.write(_msg_header(symbol, name_style, show_ts=self._show_timestamps))
        self._last_speaker = speaker
        self._last_speaker_at = now

    def _maybe_write_inline_header_body(
        self,
        speaker: str,
        symbol: str,
        name_style: str,
        body_text: Text,
    ) -> None:
        """Write header + body inline (#646 Claude Code style) with col-8 hanging indent.

        When a new turn starts (new speaker or _GROUP_WINDOW_S expired):
          - Emits ``HH:MM <sym> <body_first_line>`` at column 0.
          - Emits each body-wrap continuation at ``_current_body_indent()``
            columns so the wrap visually nests under the body text, not the
            symbol.

        When within the same speaker's grouping window (= header suppressed):
          - Emits body only via ``_write_body`` (= hanging-indent Padding),
            same as the original 2-line path.  The symbol is intentionally
            absent so successive messages from the same speaker don't pile up
            repeated headers.

        Body wrap is computed by splitting ``body_text`` at
        ``pane_width - indent`` cell-width.  The pane width falls back to
        ``_FOLD_WIDTH_FALLBACK + indent`` when ``self.size.width`` is 0
        (= widget not yet laid-out or test harness with ``size=(0,0)``).

        This is the load-bearing writer for ``render_user_message`` and
        ``_render_agent_markdown`` (plain-text first-line path).
        """
        now = time.time()
        log = self._log()
        is_new_turn = self._check_new_turn(speaker, log)
        self._last_speaker = speaker
        self._last_speaker_at = now

        indent = self._current_body_indent()

        if not is_new_turn:
            # Grouped turn: no header, just body at hanging-indent.
            self._write_body(body_text)
            return

        # New turn: build inline header prefix.
        prefix = _build_header_prefix(symbol, name_style, show_ts=self._show_timestamps)
        prefix_cells = cell_len(prefix.plain)

        # Compute body wrap width = pane_width minus the indent column.
        # The first body line starts at col ``prefix_cells`` (= same as
        # ``indent`` by design: both are 8 with ts-on, 2 with ts-off).
        try:
            pane_width = self.size.width
            if pane_width <= 0:
                pane_width = _FOLD_WIDTH_FALLBACK + indent
        except Exception:
            pane_width = _FOLD_WIDTH_FALLBACK + indent
        # RichLog has 1-cell padding on each side (see CSS: padding: 0 1).
        body_width = max(10, pane_width - indent - 2)

        # Split body_text into wrap lines at body_width.
        try:
            from rich.console import Console as _Console
            _buf_console = _Console(width=body_width, highlight=False)
            wrapped_lines = list(body_text.wrap(_buf_console, body_width))
        except Exception:
            wrapped_lines = [body_text]

        if not wrapped_lines:
            # Empty body: emit just the header prefix as a standalone line.
            log.write(prefix)
            return

        # First line: header prefix + first body-wrap line inline.
        first_line = prefix + wrapped_lines[0]
        log.write(first_line)

        # Remaining wrap lines: indented to ``indent`` col via Padding.
        for cont in wrapped_lines[1:]:
            if cont.plain.strip():  # skip blank-only continuation lines
                log.write(_indent_body(cont, indent))

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
            # W13 A#2: derive short keys from full meta when missing.
            # forwarder.py populates meta["skill_name"] + meta["run_id_short"]
            # for skill-context errors; direct router emissions (classify path,
            # chain_timeout, chain_peer_discarded) only set meta["skill"]
            # (full name) and meta["run_id"] (full id). Deriving here at the
            # TUI seam restores the [skill#abcd] prefix + re-enables the
            # Ctrl+B trace hint footer for all router-emitted errors.
            skill_name = str(
                msg.meta.get("skill_name") or msg.meta.get("skill", "")
            )
            run_id_raw = str(msg.meta.get("run_id", ""))
            run_id_short = str(
                msg.meta.get("run_id_short") or (run_id_raw[-4:] if run_id_raw else "")
            )
            details = str(msg.meta.get("details", ""))
            # W13 A#1: when details is empty, build context lines from
            # well-known meta keys so the expand region surfaces structured
            # provenance rather than just repeating the header message.
            context_lines: list[str] = []
            if not details:
                for key in ("chain_id", "skill", "run_id", "dimension"):
                    val = msg.meta.get(key)
                    if val:
                        context_lines.append(f"{key}={val}")
            self.mount_error(
                message=msg.text,
                details=details,
                run_id_short=run_id_short,
                skill_name=skill_name,
                context_lines=context_lines,
                meta=msg.meta,
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

        #646 Claude Code-style inline layout: ``HH:MM > message text`` on the
        same logical line, with wrap continuations landing at col 8
        (``_BODY_INDENT_WITH_TS``) so they nest visually under the body text.
        """
        self._snap_to_bottom()
        self._consume_empty_hint()
        self._maybe_write_inline_header_body(
            "you", _GLYPH_USER, "bold #4abbb5", Text(text),
        )
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
        self.stop_thinking()
        self.hide_status()
        text = msg.text or ""
        if _is_lifecycle_marker(text):
            self._write_log(_render_lifecycle_marker(text))
            return
        self._maybe_write_header("system", _GLYPH_SYSTEM, "bold #888888")
        for line in text.splitlines() or [""]:
            self._write_body(Text(line))
        self._write_log(Text(""))

    def _render_agent_markdown(self, msg: OutboxMessage) -> None:
        """Render a non-streaming agent message inline in the log.

        Writes Markdown directly into the RichLog (as a Rich renderable) so
        agent replies appear under their header instead of being pushed to
        the bottom of the pane. Hides any sticky "thinking…" indicator that
        was active for this turn.

        #646 Claude Code-style inline layout: ``HH:MM ⏺ [meta] first_line``
        appears on the same logical line.  The first plain-text line of the
        message body (or the meta prefix when present) is extracted and
        inlined with the header via ``_maybe_write_inline_header_body``.
        The remaining Markdown body (if any) is written at col-8 indent.

        Implementation note (structural vs simple tradeoff):
          - Markdown formatting on the first line is rendered as plain text
            (e.g. ``**bold**`` shows as plain ``bold``). For the typical
            agent reply, the first line is prose — this is an acceptable
            trade-off that avoids reimplementing Rich's Markdown renderer.
          - When no body text is present (= header-only turn signal), only
            the header prefix is emitted.
        """
        self._consume_empty_hint()
        self.stop_thinking()  # turn finished — unmount inline spinner
        self.hide_status()    # also clear any sticky status
        meta_pfx = _meta_prefix(msg.meta)
        body_text = msg.text or ""

        # Build the inline first-line text: meta prefix (if any) + first body
        # line stripped of Markdown markup.  For an agent reply that begins
        # "**Plan**:" this reads "Plan:" in the inline header — acceptable.
        if meta_pfx:
            first_line_plain = meta_pfx.rstrip()
            if body_text:
                # Append first source line (stripped of leading # / * / `) to
                # keep the header line short and readable.
                src_first = body_text.splitlines()[0].strip().lstrip("#* `_")
                if src_first:
                    first_line_plain = f"{first_line_plain} {src_first}"
        elif body_text:
            first_line_plain = body_text.splitlines()[0].strip().lstrip("#* `_")
        else:
            first_line_plain = ""

        inline_body = Text(first_line_plain, style="dim #888888" if meta_pfx else "")

        # _AMBER for agent identity — distinct from _CORAL (interactive
        # affordances).
        self._maybe_write_inline_header_body(
            "reyn", _GLYPH_AGENT, "bold " + _AMBER, inline_body,
        )

        # Write the full Markdown body (all lines) at hanging-indent.
        # This causes the first line to appear twice when body_text has one
        # line, but for Markdown replies the full render at col 8 gives
        # correct formatting (= bold, code, lists) for lines 2+.
        # When body has only one line, suppress the duplicate body write.
        if body_text:
            body_lines = body_text.splitlines()
            if len(body_lines) > 1:
                # Remaining lines as Markdown (preserving formatting).
                rest = "\n".join(body_lines[1:])
                self._write_agent_markdown_with_fold(rest)
            # Single-line body: already rendered inline; no extra write needed.
        self._write_log(Text(""))

    # ── B3 fold (long-reply truncation + /expand) ────────────────────────────

    def _estimate_rendered_lines(self, lines: list[str]) -> int:
        """Estimate the number of terminal-screen lines ``lines`` will occupy.

        Each source line wraps to ``ceil(cell_len(line) / body_width)``
        screen lines (or 1 if empty). The body column begins at
        ``_BODY_INDENT_COLS``; we conservatively subtract that + a 2-cell
        margin from the pane width. CJK / emoji are counted in display
        cells (= 2 per glyph) via ``rich.cells.cell_len``. When the pane
        is not yet mounted ``self.size.width`` is 0, in which case we
        fall back to a typical 80-col body (73 cells after indent).
        Markdown rendering may add extra lines (= headers, blockquotes,
        code blocks add padding) — the estimate is a lower bound on
        rendered height, which is the correct direction for the fold
        guard (= over-fold a little is better than letting a 116-line
        reply through).
        """
        try:
            width = max(20, self.size.width - self._current_body_indent() - 2)
        except Exception:
            width = _FOLD_WIDTH_FALLBACK
        if width <= 0:
            width = _FOLD_WIDTH_FALLBACK
        total = 0
        for line in lines:
            cells = cell_len(line)
            if cells <= 0:
                total += 1
                continue
            total += (cells + width - 1) // width
        return total

    def _write_agent_markdown_with_fold(self, text: str) -> None:
        """Write `text` as Markdown; mount FoldableMarkdown when too long.

        Short replies (≤ _FOLD_THRESHOLD_LINES estimated rendered lines):
        rendered via _write_body directly into the RichLog.

        Long replies: a FoldableMarkdown widget is mounted as a child of
        ConversationView, AFTER the most-recent RichLog content. Collapsed
        by default (shows preview + ▶ hint). toggle_last_foldable() and
        on_click toggle state.

        Side effect: appends ``text`` to ``self._recent_replies`` (capped
        at ``_RECENT_REPLIES_MAX``) so the /copy slash command can hand
        any of the last N replies to the system clipboard.
        """
        # Always remember the full text — independent of fold thresholds.
        self._recent_replies.append(text)
        if len(self._recent_replies) > _RECENT_REPLIES_MAX:
            self._recent_replies = self._recent_replies[-_RECENT_REPLIES_MAX:]
        # Wave-10 follow-up G-F11: ``splitlines()`` instead of
        # ``split("\n")`` so CRLF / CR endings normalise correctly.
        lines = text.splitlines()
        # Wave-7 B-F2: fold decision uses an estimated rendered-screen-line
        # count rather than raw source newlines.
        if self._estimate_rendered_lines(lines) <= _FOLD_THRESHOLD_LINES:
            self._write_body(RichMarkdown(text))
            self._last_long_reply = None
            return
        preview = "\n".join(lines[:_FOLD_THRESHOLD_LINES])
        # Wave-10 follow-up G-F6: report the rendered-screen-line count
        # of the suppressed tail, not the raw source-line count.
        tail_lines = lines[_FOLD_THRESHOLD_LINES:]
        remaining = self._estimate_rendered_lines(tail_lines)
        # Mount a FoldableMarkdown widget for the long reply.
        # The widget renders preview by default and supports
        # click / F8 / /expand to toggle to full content.
        foldable = FoldableMarkdown(
            full_text=text,
            preview_text=preview,
            remaining_lines=remaining,
        )
        self._foldables.append(foldable)
        self._last_long_reply = "\n".join(tail_lines)  # kept for has_pending_expand
        self.mount(foldable)

    def toggle_last_foldable(self) -> bool:
        """Toggle the latest FoldableMarkdown widget (expand ↔ collapse).

        Returns True when a widget was toggled, False when no foldable
        exists yet in this session (= "nothing to expand" path for /expand).
        """
        if not self._foldables:
            return False
        self._foldables[-1].toggle()
        return True

    def expand_last_reply(self) -> bool:
        """Toggle the latest foldable (= bidirectional; kept for back-compat).

        The old one-way append is superseded by FoldableMarkdown. This method
        now delegates to toggle_last_foldable() so callers wired to the old
        path continue to work (= /expand handler in app_outbox.py).
        """
        return self.toggle_last_foldable()

    @property
    def has_pending_expand(self) -> bool:
        """True when the latest reply produced a foldable (= long enough to fold).

        Mirrors the old single-slot semantics: a subsequent short reply
        clears ``_last_long_reply`` to None, so this returns False even
        though older foldable widgets from previous turns are still mounted.
        Used by tests that check whether the most-recent reply was long.
        """
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

    def find_in_buffer(
        self,
        query: str,
        *,
        regex: bool = False,
        case_sensitive: bool = False,
    ) -> list[tuple[int, str]]:
        """Return ``(line_idx, line_text)`` for every RichLog line matching ``query``.

        Search modes (off → on, two independent flags):
          - ``regex=False`` (default): substring match — fast, no
            metacharacters interpreted
          - ``regex=True``: compiled regex search via ``re.search``;
            invalid patterns raise ``re.error`` so the caller can
            surface a clear error status
          - ``case_sensitive=False`` (default): match across cases
            (= ``"Foo"`` query matches ``"foo bar"``)
          - ``case_sensitive=True``: exact-case match

        Scope: the live RichLog buffer (= what's currently
        scrollable). Lines past the ``_RICHLOG_MAX_LINES`` ring-
        buffer trim are NOT searched — for older history the user
        has the right-panel Events tab + the agent-side
        ``.reyn/events/agents/<name>/`` skill-run directories.

        Empty ``query`` returns an empty list (= caller treats this
        as "nothing to search for", usually with a usage hint).

        Each tuple's ``line_text`` carries the line's rendered
        plain text (via the Strip's ``.text`` property) so callers
        can surface a short preview alongside the line number.
        """
        q = (query or "").strip()
        if not q:
            return []
        log = self._log()
        out: list[tuple[int, str]] = []

        if regex:
            import re
            # ``re.error`` (invalid pattern) bubbles up — callers
            # catch it and surface a status; silent suppression
            # would leave the user wondering why ``/find -r foo(``
            # silently matched nothing.
            flags = 0 if case_sensitive else re.IGNORECASE
            pattern = re.compile(q, flags)
            for idx, strip in enumerate(getattr(log, "lines", []) or []):
                text = getattr(strip, "text", "") or ""
                if pattern.search(text):
                    out.append((idx, text))
            return out

        # Substring path — same lookup whether case-sensitive or
        # not; only the comparison case differs.
        needle = q if case_sensitive else q.lower()
        for idx, strip in enumerate(getattr(log, "lines", []) or []):
            text = getattr(strip, "text", "") or ""
            haystack = text if case_sensitive else text.lower()
            if needle in haystack:
                out.append((idx, text))
        return out

    def dump_buffer_text(self) -> list[str]:
        """Return the plain-text rendering of every line in the RichLog buffer.

        Sibling to :meth:`find_in_buffer` — same buffer scope (= the
        live, scrollable RichLog content; not historical events past
        ``_RICHLOG_MAX_LINES``), but returns the full ordered list
        without filtering. Used by ``/save`` to materialise the conv
        pane to a text file. Each entry is a single line (= no
        embedded newlines), already stripped of ANSI / Rich markup
        by Strip's ``.text`` property.
        """
        log = self._log()
        return [
            getattr(strip, "text", "") or ""
            for strip in (getattr(log, "lines", []) or [])
        ]

    def _write_log(self, text: Text) -> None:
        log = self._log()
        log.write(text)
        # Surface the trim warning the first time it's earned, even when
        # the user hasn't pressed Ctrl+P/N yet. The previous wiring only
        # called this from ``_jump_to_relative_anchor`` — turn navigation
        # — so a user who let the session auto-scroll past the
        # ``_RICHLOG_MAX_LINES`` boundary never saw the "earlier history
        # trimmed" signal until they happened to hit Ctrl+P. The
        # ``_trim_warned`` flag keeps it strictly one-shot per session.
        if not self._trim_warned and self._richlog_start_line(log) > 0:
            self._maybe_warn_about_trimmed_history(log)

    def _write_body(self, renderable: RenderableType) -> None:
        """Append a body renderable at the dynamic hanging-indent column.

        Wraps ``renderable`` in left-only ``Padding`` so wrap continuations
        line up under the speaker symbol and stay visually distinct from the
        column-0 turn header. The indent is 8 when timestamps are shown
        (= col 0-4 ts + col 5 space + col 6 symbol + col 7 space + col 8+
        body) and 2 when hidden (= col 0 symbol + col 1 space + col 2+ body).
        """
        self._log().write(_indent_body(renderable, self._current_body_indent()))

    # ── streaming support ─────────────────────────────────────────────────────

    def begin_stream(self, msg_id: str, agent_name: str = "") -> StreamingRow:
        """Start a streaming agent message row. Returns the row widget."""
        self._consume_empty_hint()
        # Same agent-identity styling as _render_agent_markdown (_AMBER).
        self._maybe_write_header("reyn", _GLYPH_AGENT, "bold " + _AMBER)
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
        self.stop_thinking()  # unmount inline spinner (turn reply started)
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

    def end_stream_cancelled(self, msg_id: str) -> str:
        """Seal a cancelled stream and write a visually-differentiated partial.

        Wave-9 F-F7: the previous cancel path called ``end_stream``
        which committed the partial text via the same
        ``_write_agent_markdown_with_fold`` formatting used for
        complete replies, then appended a separate ``"  ⌁ cancelled"``
        suffix line. Scrolling back through history the user couldn't
        tell which was a cancelled fragment vs a real reply — the
        partial rendered with full Markdown styling (= bold / headers /
        code blocks / etc.), and the dim suffix was easy to miss
        because it sat below the visible region for any reply taller
        than the viewport.

        The cancelled path now:
          - emits a clear ``✗ cancelled (partial reply):`` header BEFORE
            the partial text, in bold dim-red so it sits in the
            user's eyeline at the top of the fragment
          - renders the partial body as plain dim italic text (no
            Markdown). Partial text usually has half-closed code
            fences / unclosed lists / broken bold spans, so Markdown
            rendering produced wrong styling anyway — the dim plain
            text reads as "incomplete fragment".

        The normal ``end_stream`` path is unchanged; only the explicit
        cancel call site in ``action_cancel_inflight`` routes through
        here.
        """
        row = self._stream_rows.pop(msg_id, None)
        if row is None:
            return ""
        full = row.full_text()
        row.seal()
        self.stop_thinking()  # unmount inline spinner (cancelled stream)
        self.hide_status()
        if full:
            # Wave-10 G-F10: stash the partial in the recent-replies ring
            # buffer so ``/copy`` after a cancel returns the fragment the
            # user just saw streaming. Pre-fix only the normal
            # ``end_stream`` path routed through
            # ``_write_agent_markdown_with_fold`` (= the only writer to
            # ``_recent_replies``), so a cancel left the buffer carrying
            # whatever reply ran two turns earlier — ``/copy`` returned
            # the wrong content with no signal that the cancelled
            # fragment was unrecoverable. Capping mirrors the
            # ``_write_agent_markdown_with_fold`` cap (= one source of
            # truth for the buffer's bounded size).
            self._recent_replies.append(full)
            if len(self._recent_replies) > _RECENT_REPLIES_MAX:
                self._recent_replies = self._recent_replies[-_RECENT_REPLIES_MAX:]
            try:
                self._log().write(
                    Text("✗ cancelled (partial reply):", style="bold #aa6666"),
                )
                self._write_body(Text(full, style="dim italic #888888"))
            except Exception:
                self._log().write(Text(full))
        self._log().write(Text(""))
        try:
            row.remove()
        except Exception:
            pass
        return full

    # ── skill activity rows (C1+A1) ──────────────────────────────────────────

    def start_skill_row(
        self,
        run_id: str,
        skill_name: str,
        *,
        parent_run_id: str = "",
    ) -> SkillActivityRow:
        """Mount (or return existing) SkillActivityRow for a skill run.

        Suppresses the noisy `· phase started: …` trace stream by giving it
        a single ambient widget that updates in-place.

        ``parent_run_id`` (issue #210): when non-empty AND a row for that
        parent is currently mounted, the new row renders with a ``  └─ ``
        prefix so sub-skill spawns visibly nest under their parent in the
        conv pane. If the parent's row has already finished (= rotated
        out of ``_skill_rows``) the child renders as a normal root row —
        an orphaned ``└─`` connector pointing at a vanished line would
        be more confusing than no indent at all.
        """
        existing = self._skill_rows.get(run_id)
        if existing is not None:
            return existing
        self._consume_empty_hint()
        label_prefix = ""
        if parent_run_id and parent_run_id in self._skill_rows:
            label_prefix = "  └─ "
        row = SkillActivityRow(
            run_id=run_id,
            skill_name=skill_name,
            id=f"skillrow_{run_id[:8]}",
            label_prefix=label_prefix,
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

    def in_flight_skill_rows(self) -> list[SkillActivityRow]:
        """Return SkillActivityRow widgets whose skill is still running.

        Powers the F3 keyboard expand action (= toggle drill-down on
        whatever's executing right now). The mouse path can target any
        specific row by clicking it; the keyboard path needs a default
        target, and "everything currently running" is the most useful
        — typically that's a single row, but if multiple skills are
        concurrent the user can drill into all of them at once.

        Finished rows are excluded — once a skill completes, the user
        can still click it individually to expand, but F3 should not
        surprise them by toggling old finished rows that have scrolled
        far up. Returns an empty list when nothing is in flight; the
        caller surfaces a status hint in that case.
        """
        return [
            row for row in self._skill_rows.values()
            if not row._finished
        ]

    def in_flight_tool_call_rows(self) -> list[ToolCallRow]:
        """Return ToolCallRow widgets whose tool call is still running.

        Sibling to ``in_flight_skill_rows`` — the F3 keyboard expand
        action targets both. Finished tool call rows (= success /
        failure / abort terminal state) are excluded for the same
        reason: F3 should only touch what's currently executing,
        not old completed rows that may have scrolled far up.
        """
        return [
            row for row in self._tool_call_rows.values()
            if not row._finished
        ]

    # ── Tool-call rows (issue #427 step 4) ───────────────────────────────────

    def start_tool_call_row(
        self,
        op_id: str,
        tool_name: str,
        *,
        args_repr: str = "",
        parent_run_id: str = "",
    ) -> ToolCallRow | None:
        """Mount a ToolCallRow for ``op_id`` if one isn't already present.

        Returns the row (existing or newly mounted). ``op_id`` is the
        ``args_hash`` propagated through the forwarder; it correlates the
        eventual ``tool_call_completed`` / ``tool_call_failed`` outbox
        message back to this row. Empty ``op_id`` short-circuits to
        None (= consumer with no correlation id falls back to silent
        suppression rather than mounting an unkeyed row that can never
        be finalised).

        ``parent_run_id`` (F-F): when non-empty AND a SkillActivityRow
        for that run_id is currently mounted, the new row renders with
        a ``  └─ `` prefix so tool_calls visibly nest under their owning
        skill — same idiom as ``start_skill_row``'s ``parent_run_id``
        handling for sub-skill rows (issue #210). Root-level tool_calls
        (= no matching parent skill row) render with empty prefix.
        """
        if not op_id:
            return None
        existing = self._tool_call_rows.get(op_id)
        if existing is not None:
            return existing
        self._consume_empty_hint()
        label_prefix = ""
        if parent_run_id and parent_run_id in self._skill_rows:
            label_prefix = "  └─ "
            # Record the tool call on the owning SkillActivityRow so
            # its drill-down expand view can surface the call list
            # (= 2-level drill: phases × tools). Belt-and-braces over
            # ``ToolCallRow``'s own rendering — the row gets unmounted
            # on finalise, but the skill's drill-down should remember
            # what tools ran beneath it for the rest of the session.
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
            id=f"toolcall_{op_id[:8]}",
        )
        self._tool_call_rows[op_id] = row
        self.mount(row)
        return row

    def complete_tool_call_row(
        self, op_id: str, *, result_snippet: str = "",
    ) -> None:
        """Transition the row keyed by ``op_id`` to the success terminal.

        Mirrors ``finish_skill_row`` semantics: finalise the row, flush
        its rendered shape into the RichLog so scrollback / Ctrl+P/N
        can reach it, then unmount the live widget to bound DOM growth
        across long sessions. No-op when no row is mounted for ``op_id``
        (e.g. the start message was lost or the row was already
        finalised by a prior terminal).
        """
        row = self._tool_call_rows.pop(op_id, None)
        if row is None:
            return
        row.finish_success(result_snippet=result_snippet or None)
        self._flush_tool_call_row(row)

    def fail_tool_call_row(self, op_id: str, *, error: str = "") -> None:
        """Transition the row keyed by ``op_id`` to the failure terminal."""
        row = self._tool_call_rows.pop(op_id, None)
        if row is None:
            return
        row.finish_failure(reason=error)
        # W13 T2-1: record the most-recent failure before flushing so the
        # F7 keyboard drill-down can find it even after the flush timer fires.
        self._last_failed_tool_row = row
        self._flush_tool_call_row(row)

    def abort_tool_call_rows(self, reason: str = "cancelled") -> int:
        """Transition every live tool-call row to the aborted terminal.

        Called from ``ReynTUIApp.action_cancel_inflight`` so Ctrl+C
        doesn't leave orphan ``●`` spinners frozen in scroll history
        when the underlying skill task is cancelled mid-tool_call.
        Returns the count of rows that were live + sealed (= 0 when
        no tool_calls were in flight). Mirrors the streaming-row +
        skill-row seal sweeps that precede it in ``action_cancel_inflight``.
        """
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
                # Defensive: don't let one bad row stop the sweep.
                pass
        return cancelled

    def latest_failed_tool_row(self) -> "ToolCallRow | None":
        """Return the most-recent ToolCallRow that entered failure state.

        Returns the row object regardless of whether it is still mounted
        (= live widget, F7 can toggle expand) or already flushed into the
        RichLog (= widget removed via ``row.remove()``). The caller must
        check ``row.is_mounted`` or try ``row.toggle_expand()`` defensively
        to distinguish the two cases:

          - Mounted  (``is_mounted=True``)  → ``toggle_expand()`` works.
          - Flushed  (``is_mounted=False``) → surface a hint directing the
            user to Ctrl+B → Events tab for the full trace.

        Returns None when no failure has been recorded in this session
        (= all tool calls so far succeeded / aborted, or the view was
        cleared via Ctrl+L).

        W13 T2-1 public accessor.
        """
        return self._last_failed_tool_row

    def _flush_tool_call_row(self, row: ToolCallRow) -> None:
        """Render the row's two-line shape into the RichLog and unmount.

        Same flush pattern as ``finish_skill_row`` (= finished line goes
        to scroll history, live widget removed). The row's render
        helpers are pure over its state, so reading them after the
        finish_* call captures the terminal shape.

        F-H min-display-time: very fast tool_calls (= cache hits, instant
        returns) would mount + flush within a single event-loop tick,
        leaving no perceptual cue that the tool ran. When the row has
        been mounted for less than ``_TOOL_CALL_MIN_DISPLAY_S``, defer
        the flush via ``set_timer`` so each row stays visible briefly
        before transitioning into RichLog history.
        """
        elapsed = row.mounted_for_seconds()
        if elapsed < _TOOL_CALL_MIN_DISPLAY_S:
            delay = _TOOL_CALL_MIN_DISPLAY_S - elapsed
            try:
                self.app.set_timer(
                    delay, lambda: self._do_flush_tool_call_row(row),
                )
                return
            except Exception:
                # If timer scheduling fails (e.g. widget already torn
                # down), fall through to immediate flush — better
                # to land the row in history than lose it.
                pass
        self._do_flush_tool_call_row(row)

    def _do_flush_tool_call_row(self, row: ToolCallRow) -> None:
        """Actual write-to-RichLog + unmount; safe to call from a timer."""
        try:
            line1 = row._build_line1()
            line2 = row._build_line2()
            self._write_body(line1)
            if line2.plain:
                self._write_body(line2)
            row.remove()
        except Exception:
            # Same defensive stance as finish_skill_row: a flush
            # failure leaves the row mounted (= breadcrumb stays
            # visible) rather than blowing up the outbox loop.
            pass

    # ── SkillActivityRow (issue #210 / wave-7 #418) ───────────────────────────

    def finish_skill_row(
        self,
        run_id: str,
        *,
        success: bool = True,
        reason: str = "",
        aborted: bool = False,
    ) -> None:
        """Transition the row to ``✓ / ✗`` and roll it into the scroll log.

        Previously the row was only popped from ``_skill_rows`` and
        ``row.finish(...)`` was called, leaving the widget mounted in
        the ConversationView DOM forever. That had two consequences:

        - DOM accumulation: every completed skill stacked another
          mounted ``SkillActivityRow`` widget, growing layout cost
          monotonically across the session.
        - Unreachable breadcrumb: the ``✓ skill#abcd · Ns · Ctrl+B →
          agents`` line lived below the RichLog as a sibling, so
          ``Ctrl+P/N`` / ``Page_Up`` / arrow scrolling never reached
          it — the finished status was visible only while the user
          stayed at the bottom.

        Flush the finished row's renderable into the RichLog (where it
        becomes part of the scrollable history) and then remove the
        widget.
        """
        row = self._skill_rows.pop(run_id, None)
        if row is None:
            return
        row.finish(success=success, reason=reason, aborted=aborted)
        try:
            finished_text = row._build_finished()
            self._write_body(finished_text)
            row.remove()
        except Exception:
            # If anything in the flush path fails, leave the row mounted
            # — the breadcrumb stays visible (just not scrollable), which
            # is strictly better than blowing up the outbox loop.
            pass

    # ── sticky status (A3) ────────────────────────────────────────────────────

    def show_status(self, text: str, kind: str = "general", *, terminal: bool = False) -> None:
        s = self._sticky()
        if s is not None:
            s.show(text, kind=kind, terminal=terminal)

    def update_status(self, text: str) -> None:
        s = self._sticky()
        if s is not None:
            s.update_text(text)

    def hide_status(self) -> None:
        s = self._sticky()
        if s is not None:
            s.hide()

    # ── inline thinking spinner (A3b) ─────────────────────────────────────────

    _THINKING_ROW_ID = "inline-thinking-row"

    def start_thinking(self) -> None:
        """Mount an InlineThinkingRow in the conv pane flow, below the last message.

        Idempotent: calling twice mounts only one row (= second call is a
        no-op when a row is already present).
        """
        try:
            # Already mounted — idempotent.
            self.query_one(f"#{self._THINKING_ROW_ID}", InlineThinkingRow)
        except Exception:
            row = InlineThinkingRow(id=self._THINKING_ROW_ID)
            self.mount(row)

    def stop_thinking(self) -> None:
        """Unmount the InlineThinkingRow if present.

        Idempotent: calling without a prior ``start_thinking`` is a no-op.
        """
        try:
            row = self.query_one(f"#{self._THINKING_ROW_ID}", InlineThinkingRow)
            row.remove()
        except Exception:
            pass  # not mounted — idempotent

    # ── error box (A2) ────────────────────────────────────────────────────────

    def mount_error(
        self,
        *,
        message: str,
        details: str = "",
        run_id_short: str = "",
        skill_name: str = "",
        context_lines: list[str] | None = None,
        meta: dict | None = None,
    ) -> ErrorBox:
        self._consume_empty_hint()
        # F5: cap the live stack. When more than _MAX_VISIBLE_ERROR_BOXES
        # mounted boxes pile up under the conv pane, the oldest get
        # "rolled" into a dim breadcrumb in the RichLog and removed from
        # the DOM. This matches the F2 pattern for ESC-dismissed
        # ErrorBoxes (same summary + has_trace gating, same dim style)
        # so the conv log carries a coherent "✗ … (state)" record
        # regardless of whether the box was dismissed by user or
        # auto-evicted by stack pressure.
        from rich.text import Text as _RichText
        while len(self._error_boxes) >= _MAX_VISIBLE_ERROR_BOXES:
            # Auto-eviction shifts every remaining error's index down
            # by 1; the jump cursor would silently point at the wrong
            # box. Reset to -1 so the next F5 / F6 re-seeds from the
            # post-eviction newest error.
            self._error_jump_cursor = -1
            oldest = self._error_boxes.pop(0)
            old_first, _sep, _rest = (
                getattr(oldest, "_message", "") or ""
            ).partition("\n")
            if len(old_first) > 72:
                old_summary = old_first[:71] + "…"
            else:
                old_summary = old_first
            old_has_trace = bool(
                getattr(oldest, "_skill_name", "")
                or getattr(oldest, "_run_id_short", "")
            )
            try:
                oldest.remove()
            except Exception:
                pass
            old_trailer = " (see events)" if old_has_trace else ""
            self._write_log(_RichText(
                f"  ✗ {old_summary} (rolled to log){old_trailer}",
                style="dim #555555",
            ))
        # Stop the inline thinking spinner — the turn is over (it failed).
        # When the user is at the tail, a bare ``stop_thinking`` + ``hide_status``
        # is enough (they'll see the ErrorBox the next render tick). When
        # they're scrolled up reading history, the spinner vanishing was their
        # only feedback that something happened — leave a "✗ error below ↓"
        # cue so they know to scroll down. The next user submit clears it
        # via ``on_input_bar_user_submitted``'s own ``start_thinking()``.
        self.stop_thinking()  # turn ended in error — unmount inline spinner
        if self._user_scrolled:
            self.show_status("✗ error below ↓", kind="general")
        else:
            self.hide_status()
        # W13 A#3: classify severity so ErrorBox can apply a per-tier
        # border-left color.  Default meta={} keeps the classifier stable
        # for callers that don't pass meta.
        severity = _classify_error_severity(message, meta or {})
        box = ErrorBox(
            message=message,
            details=details,
            run_id_short=run_id_short,
            skill_name=skill_name,
            context_lines=context_lines,
            severity=severity,
        )
        self.mount(box)
        self._error_boxes.append(box)
        # Wave-11 B#6 — renumber the stack so every mounted box shows
        # ``[N/M]`` for its current position. Cheap (≤ ``_MAX_VISIBLE
        # _ERROR_BOXES`` rows = small loop), idempotent via
        # ``set_index_total``'s equality gate.
        self._renumber_error_boxes()
        # C-F4 (wave-8): once ≥ 2 error boxes are stacked, surface the
        # count via the sticky so the user can see at a glance that
        # one Esc per box is the dismiss path. Single-error case keeps
        # the existing ``"✗ error below ↓"`` cue (= no count noise for
        # the common path).
        self._maybe_show_error_count_status()
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

    def _renumber_error_boxes(self) -> None:
        """Push current ``[i, total]`` to every mounted ErrorBox.

        Wave-11 B#6 — called after every mutation to ``_error_boxes``
        (= new mount, dismiss, auto-eviction) so the per-box
        ``[N/M]`` header badge stays accurate. ``ErrorBox.set_index_total``
        is equality-gated (no DOM round-trip when values are
        unchanged), so re-numbering an unchanged stack is cheap.
        Index is 1-based for human readability.
        """
        total = len(self._error_boxes)
        for i, box in enumerate(self._error_boxes):
            try:
                box.set_index_total(i + 1, total)
            except Exception:
                # Box mid-teardown / DOM teardown — best-effort.
                pass

    def has_error_boxes(self) -> bool:
        """Return True if any undismissed ErrorBox remains."""
        return bool(self._error_boxes)

    def jump_to_error(self, direction: int) -> bool:
        """Scroll the conv pane to the next / previous mounted ErrorBox.

        ``direction``: ``+1`` for newer (forward through the list),
        ``-1`` for older (backward). The list ordering is chronological
        (= newest at end), so "forward" walks toward the most recent
        error.

        First call (= cursor unset) targets the NEWEST error (=
        ``_error_boxes[-1]``) regardless of direction — that's almost
        always the one the user wants to investigate first. Subsequent
        calls cycle with wrap.

        Returns True when a jump happened, False when no errors are
        mounted. The caller surfaces a status hint in the False case;
        the cursor advance + scroll happen inside this method.
        """
        if not self._error_boxes:
            return False
        if self._error_jump_cursor < 0 or self._error_jump_cursor >= len(self._error_boxes):
            self._error_jump_cursor = len(self._error_boxes) - 1
        else:
            self._error_jump_cursor = (
                self._error_jump_cursor + direction
            ) % len(self._error_boxes)
        target = self._error_boxes[self._error_jump_cursor]
        try:
            target.scroll_visible()
        except Exception:
            pass
        # Suppress auto-scroll so the next outbox tick doesn't yank
        # the view back to the tail.
        self._user_scrolled = True
        return True

    def error_jump_cursor(self) -> int:
        """Public read of the current error-jump cursor index (= test hook).

        Returns -1 when the cursor is unset (= no jump fired yet) or
        when the last dismissal invalidated it. Otherwise the
        0-based index into ``_error_boxes`` of the last jump target.
        """
        return self._error_jump_cursor

    def dismiss_last_error(self) -> None:
        """Remove the most recently mounted ErrorBox + leave a breadcrumb.

        UX wave F2: previously Esc removed the ErrorBox entirely with no
        trace — scrolling up later showed only the user's unanswered
        message and the failure context was gone. Now we write a dim
        one-line breadcrumb to the conv log so the dismissed failure
        stays visible in scroll history. ``(see events)`` is appended
        only when the error originated from a skill / run (= the same
        ``has_trace`` gate the ErrorBox's own ``.eb-hint`` label uses).

        Wave-10 follow-up I-F3: previously this used a ``while`` loop
        that would ``continue`` past boxes whose ``remove()`` raised
        — silently swallowing the breadcrumb for the actual "most
        recently mounted" box AND falling through to write a
        breadcrumb for the NEXT-most-recent one instead. The docstring
        promised "the most recently mounted" singular; the
        implementation was "the most recently mounted that can be
        removed without raising", with the breadcrumb pointing at the
        wrong box on remove failure.

        Restructured to a single iteration: pop the most recent box,
        write its breadcrumb FIRST (= the load-bearing record per the
        F2 intent), then best-effort ``remove()``. Removal failure no
        longer affects the breadcrumb path.
        """
        if not self._error_boxes:
            return
        # Reset the jump cursor — the list just changed shape; the next
        # F5 / F6 should re-seed from the (now newest-remaining) error
        # rather than land on a stale index.
        self._error_jump_cursor = -1
        box = self._error_boxes.pop()
        first_line, _sep, _rest = (getattr(box, "_message", "") or "").partition("\n")
        if len(first_line) > 72:
            summary = first_line[:71] + "…"
        else:
            summary = first_line
        has_trace = bool(
            getattr(box, "_skill_name", "") or getattr(box, "_run_id_short", "")
        )
        trailer = " (see events)" if has_trace else ""
        from rich.text import Text as _RichText
        # Write the breadcrumb FIRST so it lands regardless of whether
        # the subsequent remove() succeeds. The F2 intent is "scroll
        # history retains the failure context"; the DOM remove is
        # secondary cleanup.
        self._write_log(_RichText(
            f"  ✗ {summary} (dismissed){trailer}",
            style="dim #555555",
        ))
        try:
            box.remove()
        except Exception:
            # Already removed / DOM teardown in progress / etc. The
            # breadcrumb is in the log; that's the user-visible
            # contract.
            pass
        # Wave-11 B#6 — renumber surviving boxes so the badge stays
        # accurate after a dismiss. ``[2/3]`` after removing the
        # newest becomes ``[2/2]`` for the remaining tail entry.
        self._renumber_error_boxes()
        # C[1] fix: keep session._error_box_count in sync.  Defensive:
        # only decrement when the session exposes the attribute and the
        # count is already > 0 (guard against going negative on
        # double-dismiss or a session that initialises late).
        try:
            _session = self.app._get_session()  # type: ignore[attr-defined]
            if getattr(_session, "_error_box_count", None) is not None:
                if _session._error_box_count > 0:
                    _session._error_box_count -= 1
        except Exception:
            pass
        # C-F4 (wave-8): after dismiss the live count drops by 1.
        # If still ≥ 2, refresh the count sticky to the new value;
        # otherwise the count form is stale and we clear the
        # sticky so it doesn't linger as "2 errors" when only 1
        # (or 0) remains.
        n = len(self._error_boxes)
        if n >= 2:
            self._maybe_show_error_count_status()
        else:
            self.hide_status()

    def dismiss_all_errors(self) -> None:
        """Remove ALL mounted ErrorBoxes in one keystroke + emit a summary breadcrumb.

        Wave-13 B#1: when N ErrorBoxes are stacked, Esc required N
        presses. Shift+Esc binds here to clear the whole stack at once.
        A single summary breadcrumb "✗ N errors dismissed (see events)"
        lands in the conv log so the audit trail is preserved without
        flooding the log with N individual lines.
        """
        n = len(self._error_boxes)
        if n == 0:
            return
        boxes = list(self._error_boxes)
        self._error_boxes.clear()
        for box in boxes:
            try:
                box.remove()
            except Exception:
                pass
        from rich.text import Text as _RichText
        noun = "error" if n == 1 else "errors"
        self._write_log(_RichText(
            f"  ✗ {n} {noun} dismissed (see events)",
            style="dim #555555",
        ))
        # C[1] fix: zero out session._error_box_count.  Defensive guard.
        try:
            _session = self.app._get_session()  # type: ignore[attr-defined]
            if getattr(_session, "_error_box_count", None) is not None:
                _session._error_box_count = 0
        except Exception:
            pass
        # Sticky count is now stale — clear it.
        self.hide_status()

    def _maybe_show_error_count_status(self) -> None:
        """Surface the live ErrorBox count via the sticky when ≥ 2 stacked.

        C-F4 (wave-8): the existing ``_MAX_VISIBLE_ERROR_BOXES = 3``
        cap lets an error storm stack 3 boxes each requiring its own
        Esc to dismiss. Before this helper there was no at-a-glance
        count + no clue that Esc was the dismiss key. The sticky now
        reads ``✗ N errors — Esc=1, ⇧Esc=all`` when ≥ 2 boxes are
        live (Wave-13 B#1: updated to hint at the bulk-dismiss shortcut).
        The < 2 case is intentionally untouched here so the
        ``mount_error`` single-error sticky (``"✗ error below ↓"``)
        is preserved; ``dismiss_last_error`` clears the stale count
        sticky directly when it drops the live count below 2.
        """
        n = len(self._error_boxes)
        if n >= 2:
            self.show_status(
                f"✗ {n} errors — Esc=1, ⇧Esc=all", kind="general",
            )

    # ── intervention mounting ─────────────────────────────────────────────────

    def mount_intervention(
        self,
        *,
        question: str,
        choices: list[tuple[str, str] | dict] | None = None,
        answer_callback=None,
        iv_id: str = "",
        queued_extra: int = 0,
        detail: str | None = None,
        source_agent: str | None = None,
    ) -> InterventionWidget:
        self._consume_empty_hint()
        # The run is now blocked on the user's answer; "thinking…" is no
        # longer accurate. When the user is at the tail they'll see the
        # widget on the next render — bare ``hide_status`` suffices.
        # When they're scrolled up, the sticky was their only signal
        # something was happening, and ``hide_status`` would silently
        # erase it. Replace with a "⚑ intervention below ↓" cue so the
        # user knows the run is waiting for them. The natural
        # ``intervention_resolved`` outbox handler (in app_outbox.py)
        # calls ``hide_status`` on both answer paths, so the cue clears
        # itself when the user responds.
        if self._user_scrolled:
            self.show_status("⚑ intervention below ↓", kind="general")
        else:
            self.hide_status()
        widget = InterventionWidget(
            question=question,
            choices=choices,
            answer_callback=answer_callback,
            iv_id=iv_id,
            queued_extra=queued_extra,
            detail=detail,
            source_agent=source_agent,
        )
        self.mount(widget)
        # Only yank the user down to the new widget when they were
        # already at the tail. If they've scrolled up to read history,
        # the sticky just got replaced with "⚑ intervention below ↓"
        # (above) so they have a clear signal the run is waiting; the
        # widget itself becomes visible on their next scroll-down.
        if not self._user_scrolled:
            try:
                widget.scroll_visible()
            except Exception:
                pass
        return widget

    # ── cost suffix (A4) ──────────────────────────────────────────────────────

    def render_cost_suffix(
        self,
        tokens: int,
        cost_usd: float,
        elapsed_s: float,
        *,
        partial: bool = False,
    ) -> None:
        """Append a dim per-turn cost suffix, right-aligned. Caller decides when (opt-in).

        Three pieces are load-bearing:

        - ``Text(..., justify="right")`` only right-aligns when the
          renderer is told a width to fill, and ``RichLog.write`` defaults
          to ``expand=False`` — without ``expand=True`` the suffix
          silently renders at column 0.
        - Wrapping the Text in ``Padding(text, (0, RIGHT_PAD, 0, 0))``
          reserves explicit right-margin cells. Without that margin the
          right-justified end of the suffix landed under the vertical
          scrollbar + the small right-padding RichLog reserves and the
          trailing ``N.Ns`` segment was clipped at 80-col terminal widths
          (the ``expand=True`` fill width didn't subtract the scrollbar
          column from the right edge of the right-justified text).
        - Separator is ``│`` (U+2502, narrow, unambiguous width) rather
          than ``·`` (U+00B7, East Asian Width "Ambiguous") so Rich's
          cell-width accounting matches what the terminal actually paints.

        ``partial=True`` prefixes each numeric segment with ``~`` and
        appends ``  (skill still running)``. Wave-6 ST3 + wave-7 C-F6:
        when the cost-suffix deferral cap fires while a skill is still
        spinning, the snapshot under-reports the eventual total. Without
        a visual marker, the user sees ``⌁ Nt │ $X.XXXX │ Ys`` and
        treats it as the final number. The ``~`` + suffix make the
        partial nature visible at a glance; a subsequent terminal-state
        emit overrides this line with the final number.
        """
        from rich.padding import Padding
        if partial:
            body = (
                f"⌁ ~{tokens}t │ ~${cost_usd:.4f} │ ~{elapsed_s:.1f}s"
                "  (skill still running)"
            )
        else:
            body = f"⌁ {tokens}t │ ${cost_usd:.4f} │ {elapsed_s:.1f}s"
        t = Text(
            body,
            style="dim #666666",
            justify="right",
        )
        # ``(top=0, right=6, bottom=0, left=0)`` — reserve 6 cells on
        # the right so the right-justified text stays inside the
        # scrollbar + widget right-padding. Empirical calibration at
        # 80-col widths: 2 cells still clipped (``17.``), 3 still clipped
        # (``17.1.``), 6 leaves the full ``N.Ns`` segment + a visible gap
        # from the scrollbar. The ``expand=True`` fill width doesn't
        # subtract the scrollbar column or RichLog's own right-padding
        # from the right edge of the right-justified text, so we have to
        # reserve it manually.
        self._log().write(Padding(t, (0, 6, 0, 0)), expand=True)

    # ── turn navigation (B4) ──────────────────────────────────────────────────

    def jump_prev_turn(self) -> None:
        """Scroll the log to the previous agent turn anchor."""
        self._jump_to_relative_anchor(-1)

    def jump_next_turn(self) -> None:
        """Scroll the log to the next agent turn anchor."""
        self._jump_to_relative_anchor(+1)

    def _richlog_start_line(self, log: RichLog) -> int:
        """Return ``log._start_line`` defensively (G-F15, wave-10 follow-up).

        ``_start_line`` is a private RichLog attribute carrying the
        cumulative dropped-lines counter — load-bearing for the
        absolute-anchor system that powers Ctrl+P/N turn navigation.
        Textual hasn't renamed or restructured this attribute in any
        version we've tested, but relying on a private name means a
        future upgrade could silently swap it for a new public
        property. The bare ``getattr(log, "_start_line", 0)`` form
        would quietly return 0, making all anchor positions degrade
        to "treated as never-trimmed" — on a long session past the
        ``_RICHLOG_MAX_LINES`` boundary, every Ctrl+P/N would then
        land on the wrong line, and the trim-warning would never
        fire either.

        The helper logs a one-shot warning when the attribute is
        missing so operators get a detectable signal that the
        integration is broken. Behavioural fallback stays 0 (= same
        as the bare ``getattr`` default) so a no-history session
        keeps working.
        """
        value = getattr(log, "_start_line", None)
        if value is None:
            if not self._start_line_warned:
                self._start_line_warned = True
                logger.warning(
                    "RichLog._start_line is missing — Textual API may "
                    "have changed; turn-navigation anchors will degrade "
                    "to treating all history as never-trimmed. Update "
                    "the helper at conversation._richlog_start_line.",
                )
            return 0
        return value

    def _absolute_line_position(self, log: RichLog) -> int:
        """Return the absolute write-position (drop-aware) for the next line.

        ``log._start_line`` is RichLog's cumulative dropped-lines counter
        (private but stable). Combined with ``len(log.lines)``, it yields
        a monotonic absolute index that survives the ring-buffer trim —
        unlike the bare ``len(log.lines)`` value, which silently rebases
        the moment ``max_lines`` is exceeded.

        Reads through ``_richlog_start_line`` for the one-shot
        Textual-API-change warning (G-F15).
        """
        return self._richlog_start_line(log) + len(log.lines)

    def _resolve_anchors_to_current_view(self, log: RichLog) -> list[int]:
        """Project stored absolute anchors back into current ``log.lines`` indexes.

        Anchors whose target line has been trimmed (= ``absolute - start < 0``)
        are silently dropped: jumping to a turn that no longer exists in the
        log would scroll to whatever line happens to occupy that slot now,
        which is exactly the bug the bare-index version exhibited.
        """
        start = self._richlog_start_line(log)
        return [a - start for a in self._turn_anchors if a - start >= 0]

    def _maybe_warn_about_trimmed_history(self, log: RichLog) -> None:
        """Surface a one-shot warning when older history has been trimmed.

        We only fire once per session — repeated Ctrl+P presses past the
        top would otherwise spam the message. ``/clear`` resets the
        flag so a fresh session can warn again.

        Wave-10 G-F8: the warning previously wrote ONLY to the sticky
        status. The same call stack then invoked ``_flash_turn_position``
        which overwrote the sticky with ``↑ turn 1 / N`` before the user
        could read the warning — effectively making it invisible. The
        sticky path is kept as a glance-cue, but the load-bearing
        record now lives in the log as a permanent dim line so the user
        can find it in scrollback even after the sticky has been
        replaced. Same idiom as ``_render_system_message`` for one-shot
        notices that must survive subsequent sticky updates.
        """
        if self._trim_warned:
            return
        start = self._richlog_start_line(log)
        if start <= 0:
            return
        self._trim_warned = True
        warning_text = f"↑ earlier history trimmed ({start:,} lines)"
        # Permanent log line — survives the turn-flash sticky overwrite.
        try:
            self._write_log(Text(f"  {warning_text}", style="dim italic #888888"))
        except Exception:
            pass
        # Sticky glance-cue — may be overwritten by the next status
        # update, but useful at the moment the user actually hit the
        # boundary.
        sticky = self._sticky()
        if sticky is not None:
            try:
                sticky.show(warning_text, kind="general")
            except Exception:
                pass

    def scroll_page_up(self) -> None:
        """Scroll the conv log up one page without changing focus.

        Wave-4 AR5: the RichLog has ``can_focus=False`` (intentional —
        prevents inadvertent focus capture from input), so Textual's
        default PageUp doesn't reach it. The App's PageUp binding
        dispatches here to drive ``log.scroll_page_up`` directly.
        Sets the user-scrolled flag so the scroll watcher doesn't
        immediately auto-scroll back to the bottom on the next
        message.
        """
        log = self._log()
        try:
            log.scroll_page_up(animate=False)
        except Exception:
            try:
                log.scroll_relative(y=-log.size.height, animate=False)
            except Exception:
                pass
        self._user_scrolled = True

    def scroll_page_down(self) -> None:
        """Scroll the conv log down one page without changing focus."""
        log = self._log()
        try:
            log.scroll_page_down(animate=False)
        except Exception:
            try:
                log.scroll_relative(y=log.size.height, animate=False)
            except Exception:
                pass
        # When we scroll back to the tail, re-arm auto-scroll.
        try:
            if log.scroll_y >= log.max_scroll_y - 1:
                self._user_scrolled = False
        except Exception:
            pass

    def scroll_line_up(self) -> None:
        """Scroll the conv log up one line without changing focus.

        Complements ``scroll_page_up`` for fine-grained navigation when
        a single PageUp overshoots. Sets ``_user_scrolled`` so the
        scroll watcher doesn't auto-scroll back to bottom on the next
        message.
        """
        log = self._log()
        try:
            log.scroll_relative(y=-1, animate=False)
        except Exception:
            pass
        self._user_scrolled = True

    def scroll_line_down(self) -> None:
        """Scroll the conv log down one line without changing focus."""
        log = self._log()
        try:
            log.scroll_relative(y=1, animate=False)
        except Exception:
            pass
        try:
            if log.scroll_y >= log.max_scroll_y - 1:
                self._user_scrolled = False
        except Exception:
            pass

    def scroll_to_bottom(self) -> None:
        """Jump to the tail of the conv log and re-arm auto-scroll.

        Lets the user return to the live tail after reading history
        via PageUp / Ctrl+P without having to either press PageDown
        repeatedly or type a new message (which is what currently
        triggers ``_snap_to_bottom``).

        Wave-10 follow-up G-F14: the previous ``self._user_scrolled =
        False`` here was dead code — ``_snap_to_bottom`` already
        unconditionally resets the flag. Removing it eliminates the
        misleading hint that ``_snap_to_bottom`` might NOT reset the
        flag (= which a future reader would have to verify before
        editing either method).
        """
        self._snap_to_bottom()

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
            hit_boundary = target is None
            if target is None:
                target = anchors[0]
        else:
            target = None
            for a in anchors:
                if a > cur_y + 1:  # strictly below current view
                    target = a
                    break
            hit_boundary = target is None
            if target is None:
                target = anchors[-1]
        # Wave-3 FS2: when Ctrl+P/N hits the boundary AND the cursor was
        # already there, the scroll is a no-op AND the
        # ``_flash_turn_position`` dedup suppresses the turn-number
        # flash — silent. Surface a brief "↑ beginning / ↓ end of
        # history" cue so the user knows the key registered, not
        # that nav broke. ``abs(cur_y - target) <= 1`` matches the
        # same 1-line tolerance the scan loop above uses for "strictly
        # above / below".
        if hit_boundary and abs(cur_y - target) <= 1:
            self._flash_boundary_hint("start" if delta < 0 else "end")
            return
        try:
            log.scroll_to(y=target, animate=False)
        except Exception:
            pass
        # Wave-10 G-F4: mark the jump as user-initiated so the next
        # incoming chunk doesn't re-arm auto_scroll and snap the view
        # back to the tail. ``scroll_page_up`` / ``scroll_line_up``
        # already do this explicitly; ``_jump_to_relative_anchor``
        # had been relying on ``_on_log_scroll_y`` to set the flag as
        # a side effect of the scroll. The watcher path works for
        # upward jumps but flips the flag back to False whenever
        # ``at_bottom`` evaluates True — which can happen when the
        # jump target sits within 1 line of ``max_scroll_y`` (= the
        # last anchor in a recent session). The result was: Ctrl+P
        # mid-stream → view jumped → next chunk's auto_scroll write
        # immediately yanked the view back to the bottom, interrupting
        # the user's turn-navigation read. Setting the flag here
        # makes the behaviour match the explicit-scroll handlers.
        self._user_scrolled = True
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
        self._flash_turn_position(idx + 1, len(anchors), delta=delta)

    def _flash_boundary_hint(self, direction: str) -> None:
        """Surface a ``↑ beginning of history`` / ``↓ end of history`` cue.

        Wave-3 FS2: separate from ``_flash_turn_position`` so the
        boundary cue isn't deduped by the (idx, total) tuple that
        stays unchanged when the user mashes Ctrl+P at the first
        turn. Has its own dedup state so rapid repeats at the same
        boundary don't spam — but the moment the user navigates
        inward (= turn-flash fires), the dedup clears so re-hitting
        the boundary later flashes again.

        Wave-10 follow-up G-F9: was previously dual-write (=
        ``_write_log`` permanent breadcrumb + ``show_status``
        sticky). The log line polluted scrollback — alternating
        Ctrl+P / Ctrl+N at boundaries wrote two new ``↑ beginning``
        / ``↓ end`` lines on every direction change, accumulating
        as navigation artifacts indistinguishable from actual
        conversation content. ``_flash_turn_position`` already
        switched to sticky-only after FS1 fixed the visibility
        gap; the sticky is docked at the conv pane's bottom and is
        ALWAYS visible regardless of scroll position, so the log
        line was redundant rather than a fallback. Drop the log
        write; sticky alone is sufficient.
        """
        if direction == "start":
            text = "↑ beginning of history"
        else:
            text = "↓ end of history"
        # Dedup against the previous boundary direction. Mashing
        # Ctrl+P at the start just shows the hint once.
        if self._last_boundary_flash == direction:
            return
        self._last_boundary_flash = direction
        # Clear the turn-flash dedup so a subsequent in-bounds
        # Ctrl+P/N re-flashes the turn number cleanly.
        self._last_turn_flash = None
        # Sticky-only surface — visible regardless of scroll
        # position. ``kind="general"`` reads as advisory (= grey
        # accent) without preempting an active ``⟳ thinking…``
        # (= the wave-10 G-F8 + I-F8 priority guard handles that).
        self.show_status(text, kind="general")

    def _flash_turn_position(self, n: int, total: int, delta: int = -1) -> None:
        """Surface ``↑/↓ turn N / M`` feedback for Ctrl+P/N navigation.

        ``delta`` selects the direction arrow: negative (Ctrl+P / backward
        toward earlier history) → ``↑``, positive (Ctrl+N / forward
        toward newer history) → ``↓``. Pre-fix the arrow was hard-coded
        to ``↑`` regardless of direction (G-F5, wave-10) — pressing
        Ctrl+N to advance forward through turns showed
        ``↑ turn 5 / 8``, which contradicts the actual movement and
        misleads users who use the arrow as a navigation cue.

        Deduped by ``_last_turn_flash`` so rapid Ctrl+P/N presses that
        land on the same anchor don't spam the surface with identical
        messages. Cleared on Ctrl+L so post-clear navigation flashes
        fresh.

        Wave-3 FS1: previously this wrote ONLY to the conv log via
        ``_write_log``. Problem: Ctrl+P scrolls the user UP through
        history, and the new turn-N/M line lands at the LOG BOTTOM
        (= invisible at the current scroll position). The user
        navigates to turn 1 of 8 but only sees the flash when they
        later scroll back to the bottom — by which time the lines
        have accumulated as junk in conversation history.

        Switch to the sticky-status surface (= the same place the
        boundary hint from FS2 writes to). It's visible regardless
        of scroll position AND it doesn't pollute the conv log
        permanently. ``kind="general"`` reads as advisory (grey
        accent), doesn't preempt an active ``⟳ thinking…`` (= those
        use ``kind="thinking"``).

        Boundary dedup state also cleared here so any successful
        in-bounds move re-flashes the boundary cue next time.
        """
        if self._last_turn_flash == (n, total):
            return
        self._last_turn_flash = (n, total)
        # Wave-3 FS2: any successful in-bounds move clears the boundary
        # dedup so the next time the user hits the edge they get the
        # hint fresh (= "user navigated away from the boundary").
        self._last_boundary_flash = None
        arrow = "↑" if delta < 0 else "↓"
        self.show_status(f"{arrow} turn {n} / {total}", kind="general")

    def clear(self) -> None:
        """Ctrl+L: clear the log + reset state. Does not affect engine state."""
        # Wave-13 C#1: capture the live error count BEFORE clearing so we can
        # emit an audit breadcrumb AFTER the log wipe. The breadcrumb lands in
        # the freshly-blank log so it survives the clear and points the user at
        # the events tab for full context.
        _error_count_before_clear = len(self._error_boxes)
        self._log().clear()
        for row in self._stream_rows.values():
            row.seal()
        self._stream_rows.clear()
        # Force-finish any in-progress skill rows so they don't keep ticking
        for row in list(self._skill_rows.values()):
            row.finish(success=True, reason="cleared")
        self._skill_rows.clear()
        # Remove any leftover ErrorBox widgets. They mount as children of
        # the conv pane (not lines in the RichLog), so ``_log().clear()``
        # above doesn't touch them — without this loop the boxes float on
        # an otherwise-blank conv pane until the user hits Esc per box.
        for box in self._error_boxes:
            try:
                box.remove()
            except Exception:
                pass
        self._error_boxes.clear()
        # C[1] fix: zero out session._error_box_count after all boxes
        # are gone.  Defensive guard — _get_session() may return None.
        try:
            _session = self.app._get_session()  # type: ignore[attr-defined]
            if getattr(_session, "_error_box_count", None) is not None:
                _session._error_box_count = 0
        except Exception:
            pass
        # Wave-13 C#1: post-clear audit breadcrumb. Written AFTER _log().clear()
        # so it survives in the fresh log (writing before clear() would wipe it
        # along with the rest of the history). The message is intentionally
        # brief and dim — it's a pointer to the events tab, not a summary.
        if _error_count_before_clear > 0:
            from rich.text import Text as _RichText
            _noun = "error" if _error_count_before_clear == 1 else "errors"
            self._write_log(_RichText(
                f"  ✗ {_error_count_before_clear} {_noun} cleared"
                " (see events tab, filter=error)",
                style="dim #555555",
            ))
        # Wave-10 G-F1: sweep in-flight ToolCallRow widgets too. They
        # share the same "child of ConversationView, not a RichLog
        # line" mounting model as ErrorBox / StreamingRow / SkillActivityRow,
        # so ``_log().clear()`` doesn't unmount them. Without this loop:
        #   - the row widgets stay on screen as orphans on the now-blank
        #     pane (= same visual artefact ErrorBox suffered before its
        #     sweep was added)
        #   - ``_tool_call_rows`` still carries the stale op_id keys, so
        #     when the in-flight tool finally completes,
        #     ``complete_tool_call_row`` / ``fail_tool_call_row`` pops
        #     the dict entry and calls ``row.remove()`` against an
        #     already-orphaned widget → Textual DOM exception swallowed
        #     by the bare ``except Exception: pass`` in the caller.
        # We use ``finish_aborted("cleared")`` rather than a bare
        # ``remove()`` so the row briefly renders its ⊘ terminal state
        # before the parent ``clear()`` blanks the log — matches the
        # ``_skill_rows`` ``finish(reason="cleared")`` idiom directly
        # above.
        for row in list(self._tool_call_rows.values()):
            try:
                row.finish_aborted("cleared")
                row.remove()
            except Exception:
                pass
        self._tool_call_rows.clear()
        # W13 T2-1: reset the most-recent failure reference so F7 on a
        # fresh-cleared pane correctly shows "no recent tool failure".
        self._last_failed_tool_row = None
        # AsyncStackPanel wiring: drop all bottom-stack entries so a
        # Ctrl+L leaves the panel empty alongside the conv log.
        # Without this the running-task overview would survive the
        # clear and look like ghost rows attached to an otherwise-
        # fresh pane. Same pattern as the ErrorBox / ToolCallRow
        # sweeps directly above.
        self.clear_async_tasks()
        # Wave-10 G-F2: sweep any pending InterventionWidget children too.
        # ``mount_intervention`` adds the widget via ``self.mount(widget)``
        # with no tracking list, so the only way to find them at clear()
        # time is ``self.query(InterventionWidget)``. Without this sweep,
        # a Ctrl+L while an intervention modal is open leaves the chip
        # buttons floating on a blank pane — the user can still "answer"
        # the question, firing the answer_callback against a session
        # context the user just cleared (= acting on stale UI state). The
        # sticky ``⚑ intervention below ↓`` set by ``mount_intervention``
        # is already hidden by the ``hide_status`` call below; this loop
        # removes the visible widget too.
        for widget in list(self.query(InterventionWidget)):
            try:
                widget.remove()
            except Exception:
                pass
        # Reset header-grouping + turn anchors + fold stash
        self._last_speaker = ""
        self._last_speaker_at = 0.0
        # Wave-10 follow-up G-F13: preserve today's date as the
        # "last separator date" so a same-day Ctrl+L doesn't emit
        # another ``── YYYY-MM-DD ──`` separator on the next message.
        # The separator is a *day-boundary* marker, not a session-start
        # marker — emitting it every clear made it appear multiple
        # times per day purely because the user cleared the pane.
        # ``""`` is the cold-start value (= "no date written yet"), and
        # the first message's ``_maybe_write_header`` would emit the
        # separator unconditionally. Setting to today's date makes that
        # branch a no-op for same-day clears while leaving day-crossing
        # clears (= rare but possible if the user clears around midnight)
        # correct.
        self._last_header_date = time.strftime("%Y-%m-%d")
        self._last_turn_flash = None
        self._last_boundary_flash = None
        self._turn_anchors.clear()
        self._trim_warned = False
        self._last_long_reply = None
        # B3 toggle: remove any mounted FoldableMarkdown widgets + clear list.
        for widget in list(self.query(FoldableMarkdown)):
            try:
                widget.remove()
            except Exception:
                pass
        self._foldables.clear()
        # Wave-10 G-F3: reset the recent-replies ring buffer. Pre-fix
        # ``/copy`` after a Ctrl+L returned replies from the now-invisible
        # prior session — confusing, and potentially surfaces content
        # the user thought they had cleared. Same lifecycle as
        # ``_last_long_reply`` directly above: both are agent-reply
        # caches that should not survive a pane clear.
        self._recent_replies.clear()
        # Re-arm auto-scroll: clear() puts the user back at a fresh blank
        # log, and any prior scroll-up state is meaningless once the
        # content it was reading is gone.
        self._user_scrolled = False
        try:
            self._log().auto_scroll = True
        except Exception:
            pass
        # Stop inline spinner and hide sticky status
        self.stop_thinking()
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
                    "agent", "system", "status", "error", "trace", "skill_done",
                    # Issue #192: hot_list_updated is a data signal (= meta
                    # carries the full ranking); no display copy in the conv
                    # pane. Consumed by the Memory tab augmentation
                    # (tui-coder follow-up).
                    "hot_list_updated"}:
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
